#!/usr/bin/python3
#
# Written by Adam Fontenot: https://github.com/afontenot
# Based on a snippet by Jonas Ådahl: https://gitlab.gnome.org/-/snippets/19

import re
import signal
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
from threading import Timer

import dbus
import gi
from dbus.mainloop.glib import DBusGMainLoop

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib


class DesktopPortalManager:
    DESKTOP_IFACE = "org.freedesktop.portal.Desktop"
    DESKTOP_PATH = "/org/freedesktop/portal/desktop"
    REQUEST_IFACE = "org.freedesktop.portal.Request"
    SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"

    def __init__(self):
        self._bus = dbus.SessionBus()
        self._request_token_counter = 0
        self._session_token_counter = 0
        self._sender_name = re.sub(r"\.", r"_", self._bus.get_unique_name()[1:])
        self._session_path, self._session_token = self._new_session_path()
        self._session = None
        self._portal = self._bus.get_object(self.DESKTOP_IFACE, self.DESKTOP_PATH)
        self._callback = None

    def _new_request_path(self):
        self._request_token_counter += 1
        token = f"u{self._request_token_counter}"
        path = f"{self.DESKTOP_PATH}/request/{self._sender_name}/{token}"
        return (path, token)

    def _new_session_path(self):
        self._session_token_counter += 1
        token = f"u{self._session_token_counter}"
        path = f"{self.DESKTOP_PATH}/session/{self._sender_name}/{token}"
        return (path, token)

    def _dbus_screencast(self, method, callback, *args, options={}):
        (request_path, request_token) = self._new_request_path()
        self._bus.add_signal_receiver(
            callback,
            "Response",
            self.REQUEST_IFACE,
            self.DESKTOP_IFACE,
            request_path,
        )
        options["handle_token"] = request_token
        method(*(args + (options,)), dbus_interface=self.SCREENCAST_IFACE)

    def _process_streams(self, response, results):
        if response != 0:
            print("Did not receive streams from portal.")
            self._callback(None)
            return

        print("Sources selected")
        self._callback(results["streams"])

    def _start_portal(self, response, results):
        if response != 0:
            print(f"Failed to select sources: {response}")
            self._callback(None)
            return

        self._dbus_screencast(
            self._portal.Start,
            self._process_streams,
            self._session,
            "",
        )

    def _select_sources(self, response, results):
        if response != 0:
            print(f"Failed to create session: {response}")
            self._callback(None)
            return

        self._session = results["session_handle"]
        print(f"session {self._session} created")

        self._dbus_screencast(
            self._portal.SelectSources,
            self._start_portal,
            self._session,
            options={"multiple": False, "types": dbus.UInt32(1 | 2)},
        )

    def get_streams(self, callback):
        self._callback = callback
        self._dbus_screencast(
            self._portal.CreateSession,
            self._select_sources,
            options={"session_handle_token": self._session_token},
        )

    def get_pipewire_fd(self):
        empty_dict = dbus.Dictionary(signature="sv")
        fd_obj = self._portal.OpenPipeWireRemote(
            self._session, empty_dict, dbus_interface=self.SCREENCAST_IFACE
        )
        return fd_obj.take()


class PipewireRecorder:
    def __init__(self, crf, vbv_maxrate, location):
        self.loop = GLib.MainLoop()
        self._dpm = DesktopPortalManager()
        self._portal = None
        self._pipeline = None
        self.crf = crf
        self.vbv_maxrate = vbv_maxrate
        self.location = location
        self._delayed_terminate = None

    def _gst_message_callback(self, bus, message):  # , _loop):
        if message.type == Gst.MessageType.EOS:
            self.terminate()
        elif message.type == Gst.MessageType.ERROR:
            err, _ = message.parse_error()

            # Clean up if the input stream ended abruptly
            if err.matches(Gst.ResourceError.quark(), Gst.ResourceError(err.code)):
                print("Stream ended, trying to quit gracefully.")
                self.delayed_terminate()

                # Note: don't send the EOS directly to the pipe
                # In cases where the pipewiresrc has died, it won't be passed through
                sink = self._pipeline.get_by_name("sink")
                sink.send_event(Gst.Event.new_eos())
            else:
                print("Error:", message.src.get_name(), err.message)

    def _record(self, node_id):
        fd = self._dpm.get_pipewire_fd()
        self._pipeline = Gst.parse_launch(
            f"pipewiresrc fd={fd} path={node_id}"
            f" ! videoconvert"
            f" ! queue"
            f" ! x264enc intra-refresh=true quantizer={self.crf} speed-preset=fast pass=qual bitrate={self.vbv_maxrate}"
            f" ! h264parse"
            f" ! matroskamux name=sink"
            f' ! filesink location="{self.location}"'
        )
        # Messages don't get caught by the bus unless you explicitly ask
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._gst_message_callback)  # , loop)

        print(f"Saving file to {self.location}")
        self._pipeline.set_state(Gst.State.PLAYING)

    def record(self):
        # callback to record each stream we get from DBus, sequentially
        def get_streams_callback(streams):
            if streams is None:
                self.terminate()
                return
            for node_id, stream_properties in streams:
                print(f"Stream {node_id}")
                self._record(node_id)

        self._dpm.get_streams(get_streams_callback)

    def softexit(self, *args):
        if self._pipeline is not None:
            print("Caught SIGINT, trying to exit gracefully.")
            self._pipeline.send_event(Gst.Event.new_eos())
            self.delayed_terminate()
        else:
            self.loop.quit()

    def delayed_terminate(self, delay=1.0):
        if self._delayed_terminate is None:
            self._delayed_terminate = Timer(delay, self.terminate)
            self._delayed_terminate.start()

    def terminate(self):
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        self.loop.quit()
        if self._delayed_terminate is not None:
            self._delayed_terminate.cancel()


def main():
    parser = ArgumentParser(description="Record your desktop from PipeWire")
    parser.add_argument(
        "--crf", type=float, help="x264 constant rate factor", default=18
    )
    parser.add_argument(
        "--maxrate",
        type=int,
        help="x264 vbv_maxrate: maximum rate at which video buffer will be filled, in kbps",
        default=10000,
    )
    parser.add_argument("-o", "--output", type=Path, help="output file location (mkv)")
    args = parser.parse_args()

    location = args.output
    if location is None:
        location = Path.home() / (datetime.now().isoformat("_", "seconds") + ".mkv")

    if not location.parent.exists():
        print(f"Selected output directory {location.parent} does not exist.")

    DBusGMainLoop(set_as_default=True)
    Gst.init(None)

    pwr = PipewireRecorder(args.crf, args.maxrate, location)
    pwr.record()

    # catch KeyboardInterrupt in a GLib loop friendly way
    signal.signal(signal.SIGINT, pwr.softexit)

    # join loop
    pwr.loop.run()


if __name__ == "__main__":
    main()

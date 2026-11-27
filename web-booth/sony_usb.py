"""Sony α7 II PC Remote over Windows' built-in WIA/PTP transport."""

import asyncio
import io
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from PIL import Image

LIVE_OBJECT = 0xFFFFC002
STILL_OBJECT = 0xFFFFC001
MAX_STILL = 64 * 1024 * 1024


def _packet(opcode, params, phase, data=b""):
    return struct.pack("<H9I", opcode, 0, 0, *params, *([0] * (5 - len(params))),
                       len(params), phase) + data


def _device_id():
    from comtypes.client import CreateObject

    manager = CreateObject("WIA.DeviceManager", dynamic=True)
    matches = []
    for item in manager.DeviceInfos:
        values = {prop.PropertyID: str(prop.Value) for prop in item.Properties
                  if prop.PropertyID in (3, 7)}
        if "sony" in values.get(3, "").lower() and values.get(7, "").upper() == "ILCE-7M2":
            matches.append(item.DeviceID)
    if len(matches) != 1:
        raise OSError("Windows must detect exactly one Sony α7 II in USB PC Remote mode.")
    return matches[0]


class _WiaTransport:
    def __init__(self):
        from ctypes import HRESULT, POINTER, c_long, c_void_p
        from comtypes import BSTR, COMMETHOD, GUID, IUnknown
        from comtypes.client import CreateObject

        class IWiaDevMgr(IUnknown):
            _iid_ = GUID("{5EB2502A-8CF1-11D1-BF92-0060081ED811}")
            _methods_ = [
                COMMETHOD([], HRESULT, "EnumDeviceInfo", (['in'], c_long, 'flags'),
                          (['out'], POINTER(POINTER(IUnknown)), 'items')),
                COMMETHOD([], HRESULT, "CreateDevice", (['in'], BSTR, 'device_id'),
                          (['out'], POINTER(POINTER(IUnknown)), 'item')),
            ]

        class IWiaItemExtras(IUnknown):
            _iid_ = GUID("{6291EF2C-36EF-4532-876A-8E132593778D}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetExtendedErrorInfo", (['out'], POINTER(BSTR), 'text')),
                COMMETHOD([], HRESULT, "Escape", (['in'], c_long, 'code'),
                          (['in'], c_void_p, 'input'), (['in'], c_long, 'input_size'),
                          (['in'], c_void_p, 'output'), (['in'], c_long, 'output_size'),
                          (['out'], POINTER(c_long), 'written')),
            ]

        device_id = _device_id()
        manager = CreateObject(GUID("{A1F4E726-8CF1-11D1-BF92-0060081ED811}"), interface=IWiaDevMgr)
        self.extra = manager.CreateDevice(device_id).QueryInterface(IWiaItemExtras)

    def transact(self, request, capacity):
        from ctypes import create_string_buffer

        buffer = create_string_buffer(capacity)
        written = self.extra.Escape(256, create_string_buffer(request), len(request), buffer, capacity)
        if not 30 <= written <= capacity:
            raise OSError("Incomplete response from Sony USB.")
        return buffer[:written]


class _PtpError(OSError):
    def __init__(self, code):
        super().__init__(f"Sony USB returned PTP status 0x{code:04x}.")
        self.code = code


def _objects_ready(data):
    """Read Sony's ObjectInMemory property; don't download an unfinished still."""
    if len(data) < 8:
        raise OSError("Incomplete Sony property list.")
    count = struct.unpack_from("<I", data)[0]
    offset = 8

    def value_size(kind, pos):
        if kind == 0xFFFF:
            return 1 + 2 * data[pos]
        if not 1 <= (kind & 0xFFF) <= 10:
            raise ValueError("Unknown Sony property type")
        size = 1 << (((kind & 0xFFF) - 1) // 2)
        return 4 + struct.unpack_from("<I", data, pos)[0] * size if kind & 0x4000 else size

    try:
        for _ in range(count):
            prop, kind = struct.unpack_from("<HH", data, offset)
            offset += 6  # Property, type, access and enable flags.
            offset += value_size(kind, offset)
            if prop == 0xD215 and kind == 4:
                return struct.unpack_from("<H", data, offset)[0]
            offset += value_size(kind, offset)
            form = data[offset]
            offset += 1
            if form == 1:
                for _ in range(3):
                    offset += value_size(kind, offset)
            elif form == 2:
                length = struct.unpack_from("<H", data, offset)[0]
                offset += 2
                for _ in range(length):
                    offset += value_size(kind, offset)
            elif form != 0:
                break
    except (IndexError, struct.error, ValueError) as exc:
        raise OSError("Could not read Sony capture status.") from exc
    raise OSError("Sony did not report capture status.")


class SonyCamera:
    def __init__(self, transport):
        self.transport = transport

    def _command(self, opcode, *params, phase=3, data=b"", capacity=65536):
        response = self.transport.transact(_packet(opcode, params, phase, data), capacity)
        code = struct.unpack_from("<H", response)[0]
        if code != 0x2001:
            raise _PtpError(code)
        return response[30:]

    def connect(self):
        self._command(0x9201, 1, 0, 0)
        time.sleep(0.15)
        self._command(0x9201, 2, 0, 0)
        # Some legacy Alpha bodies return an empty descriptor until the remote
        # session is ready. Retry only the read-only descriptor, not the shutter
        # or the state-changing handshake steps.
        for _ in range(3):
            time.sleep(0.25)
            info = self._command(0x9202, 200)
            if len(info) >= 10 and info[:2] == b"\xc8\x00":
                break
        else:
            raise OSError("The Sony did not confirm the PC Remote handshake.")
        time.sleep(0.15)
        self._command(0x9201, 3, 0, 0)
        time.sleep(0.15)

    def _object(self, handle):
        info = self._command(0x1008, handle)
        if len(info) < 12:
            raise OSError("Sony USB returned incomplete image information.")
        size = struct.unpack_from("<I", info, 8)[0]
        if not 0 < size <= MAX_STILL:
            raise OSError("Sony USB returned an invalid image size.")
        image = self._command(0x1009, handle, capacity=size + 64)
        if len(image) < size:
            raise OSError("Sony USB returned an incomplete image.")
        return image[:size]

    def preview(self):
        try:
            image = self._object(LIVE_OBJECT)
        except _PtpError as exc:
            if exc.code in (0x2009, 0x2019, 0x200F):
                return None
            raise
        offset = 0 if image.startswith(b"\xff\xd8") else int.from_bytes(image[:4], "little")
        end = image.rfind(b"\xff\xd9")
        return image[offset:end + 2] if end > offset and image[offset:offset + 2] == b"\xff\xd8" else None

    def _shutter(self, control, value):
        self._command(0x9207, control, phase=4, data=struct.pack("<H", value))

    def _ready(self):
        return _objects_ready(self._command(0x9209))

    def capture(self, destination, timeout=20):
        if self._ready():
            raise OSError("Sony still has an earlier capture pending. No new shot was fired.")
        try:
            self._shutter(0xD2C1, 2)
            time.sleep(0.3)
            self._shutter(0xD2C2, 2)
            time.sleep(0.1)
        finally:
            try:
                self._shutter(0xD2C2, 1)
            finally:
                self._shutter(0xD2C1, 1)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready() >= 0x8000:
                image = self._object(STILL_OBJECT)
                if not image.startswith(b"\xff\xd8"):
                    continue  # RAW+JPEG may transfer the RAW object first.
                with Image.open(io.BytesIO(image)) as photo:
                    if photo.format != "JPEG" or photo.width < 2000:
                        raise OSError("Sony did not transfer a full-resolution JPEG.")
                    photo.load()
                destination.write_bytes(image)
                return
            time.sleep(0.15)
        raise OSError("Sony did not transfer a new JPEG in time.")


class SonyUSB:
    """Keep camera I/O serialized and on one COM apartment."""

    def __init__(self):
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sony-usb")
        self._camera = None
        self.last_frame = 0.0

    def _run(self, operation, *args):
        import comtypes

        # Initialize once on this executor thread via a thread-local marker.
        from threading import current_thread
        thread = current_thread()
        if not getattr(thread, "sony_com_ready", False):
            comtypes.CoInitialize()
            thread.sony_com_ready = True
        if operation == "available":
            with suppress(OSError, comtypes.COMError):
                _device_id()
                return True
            return False
        try:
            if self._camera is None:
                camera = SonyCamera(_WiaTransport())
                camera.connect()
                self._camera = camera
            result = getattr(self._camera, operation)(*args)
            if operation == "preview" and result:
                self.last_frame = time.monotonic()
            return result
        except comtypes.COMError as exc:
            self._camera = None
            self.last_frame = 0.0
            raise OSError(f"Windows could not communicate with the Sony over USB ({exc.hresult & 0xffffffff:08x}).") from exc
        except OSError:
            if operation == "preview":
                self._camera = None
                self.last_frame = 0.0
            raise

    async def call(self, operation, *args):
        if os.name != "nt":
            if operation == "available":
                return False
            raise OSError("Direct Sony USB requires Windows.")
        return await asyncio.get_running_loop().run_in_executor(self._worker, self._run, operation, *args)

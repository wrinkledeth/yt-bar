import ctypes
from ctypes import byref, c_uint32, c_void_p
from dataclasses import dataclass


def _fourcc(value):
    return int.from_bytes(value.encode("ascii"), "big")


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


AUDIO_OBJECT_SYSTEM_OBJECT = 1
AUDIO_OBJECT_UNKNOWN = 0
AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL = _fourcc("glob")
AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN = 0
AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE = _fourcc("dOut")


try:
    _CORE_AUDIO = ctypes.CDLL(
        "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
    )
    _AUDIO_OBJECT_GET_PROPERTY_DATA = _CORE_AUDIO.AudioObjectGetPropertyData
    _AUDIO_OBJECT_GET_PROPERTY_DATA.argtypes = [
        c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        c_uint32,
        c_void_p,
        ctypes.POINTER(c_uint32),
        c_void_p,
    ]
    _AUDIO_OBJECT_GET_PROPERTY_DATA.restype = ctypes.c_int32

    _AUDIO_OBJECT_LISTENER_PROC = ctypes.CFUNCTYPE(
        ctypes.c_int32,
        c_uint32,
        c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        c_void_p,
    )

    _AUDIO_OBJECT_ADD_PROPERTY_LISTENER = _CORE_AUDIO.AudioObjectAddPropertyListener
    _AUDIO_OBJECT_ADD_PROPERTY_LISTENER.argtypes = [
        c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        _AUDIO_OBJECT_LISTENER_PROC,
        c_void_p,
    ]
    _AUDIO_OBJECT_ADD_PROPERTY_LISTENER.restype = ctypes.c_int32

    _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER = (
        _CORE_AUDIO.AudioObjectRemovePropertyListener
    )
    _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER.argtypes = [
        c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        _AUDIO_OBJECT_LISTENER_PROC,
        c_void_p,
    ]
    _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER.restype = ctypes.c_int32
except OSError:
    _AUDIO_OBJECT_GET_PROPERTY_DATA = None
    _AUDIO_OBJECT_LISTENER_PROC = None
    _AUDIO_OBJECT_ADD_PROPERTY_LISTENER = None
    _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER = None


@dataclass
class DefaultOutputListener:
    address: AudioObjectPropertyAddress
    callback: object


def get_default_output_device_id():
    if _AUDIO_OBJECT_GET_PROPERTY_DATA is None:
        return None

    address = AudioObjectPropertyAddress(
        AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE,
        AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
        AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    device_id = c_uint32(AUDIO_OBJECT_UNKNOWN)
    size = c_uint32(ctypes.sizeof(device_id))
    status = _AUDIO_OBJECT_GET_PROPERTY_DATA(
        AUDIO_OBJECT_SYSTEM_OBJECT,
        byref(address),
        0,
        None,
        byref(size),
        byref(device_id),
    )
    if status != 0 or device_id.value == AUDIO_OBJECT_UNKNOWN:
        return None
    return int(device_id.value)


def install_default_output_listener(callback):
    if _AUDIO_OBJECT_ADD_PROPERTY_LISTENER is None:
        return None

    address = AudioObjectPropertyAddress(
        AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE,
        AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
        AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )

    @_AUDIO_OBJECT_LISTENER_PROC
    def _listener(in_object_id, in_number_addresses, in_addresses, in_client_data):
        callback()
        return 0

    status = _AUDIO_OBJECT_ADD_PROPERTY_LISTENER(
        AUDIO_OBJECT_SYSTEM_OBJECT,
        byref(address),
        _listener,
        None,
    )
    if status == 0:
        return DefaultOutputListener(address=address, callback=_listener)

    print(f"Failed to register default output listener: {status}")
    return None


def uninstall_default_output_listener(listener):
    if (
        listener is None
        or _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER is None
        or listener.callback is None
    ):
        return

    status = _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER(
        AUDIO_OBJECT_SYSTEM_OBJECT,
        byref(listener.address),
        listener.callback,
        None,
    )
    if status != 0:
        print(f"Failed to remove default output listener: {status}")

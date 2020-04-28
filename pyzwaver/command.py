#!/usr/bin/python3
# Copyright 2016 Robert Muth <robert@muth.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 3
# of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""
command.py contain code for parsing and assembling API_APPLICATION_COMMAND
requests.

It also defines a bunch if `custom` commands that are used for administrative
purposes and have no wire representation.
"""

import logging

from pyzwaver import zwave as z

# The command below prefixed with CUSTOM_COMMAND_* are made up
CUSTOM_COMMAND_APPLICATION_UPDATE = (256, 1)   # NIFs and MultiChannel_CapabilityReports are transformed into this
CUSTOM_COMMAND_PROTOCOL_INFO      = (256, 2)   # protocol info messages are transformed into this
CUSTOM_COMMAND_ACTIVE_SCENE       = (256, 3)   # unused
CUSTOM_COMMAND_FAILED_NODE        = (256, 4)   # if API_ZW_IS_FAILED_NODE_ID reports a failed node

_CUSTOM_COMMAND_STRINGS = {
    CUSTOM_COMMAND_ACTIVE_SCENE:       "_Active_Scene",
    CUSTOM_COMMAND_APPLICATION_UPDATE: "_Application_Update",
    CUSTOM_COMMAND_PROTOCOL_INFO:      "_ProtocolInfo",
    CUSTOM_COMMAND_FAILED_NODE:        "_FailedNode"
}


def IsCustom(key):
    return key in _CUSTOM_COMMAND_STRINGS


def StringifyCommand(key):
    s = _CUSTOM_COMMAND_STRINGS.get(key)
    if s: return s
    s = z.SUBCMD_TO_STRING.get(key[0] * 256 + key[1])
    if s: return s
    return "Unknown:%02x:%02x" % (key[0], key[1])


def StringifyCommandClass(cls):
    return z.CMD_TO_STRING.get(cls, "UNKNOWN:%d" % cls)


def NodeDescription(basic_generic_specific):
    k = basic_generic_specific[1] * 256 + basic_generic_specific[2]
    v = z.GENERIC_SPECIFIC_DB.get(k)
    if v is None:
        logging.error("unknown generic device : %s", str(basic_generic_specific))
        return "unknown device_description: %s" % str(basic_generic_specific)
    return v[0]


# ======================================================================
# Parse Helpers
# ======================================================================
def _GetSignedValue(data):
    return int.from_bytes(data, 'big', signed=True)


def _SetSignedValue(value):
    mag = value if (value >= 0) else (-value-1)

    if mag <= 0x7F:
        length = 1
    elif mag <= 0x7FFF:
        length = 2
    elif mag <= 0x7FFFFFFF:
        length = 4
    else:
        raise ValueError(
            "{:} won't fit in 4-byte two's complement".format(value))
    return list(value.to_bytes(length, 'big', signed=True))


def _GetReading(m, index, units_extra):
    c = m[index]
    size = c & 0x7
    units = (c & 0x18) >> 3 | units_extra
    exp = (c & 0xe0) >> 5
    mantissa = m[index + 1: index + 1 + size]
    value = _GetSignedValue(mantissa) / pow(10, exp)
    return index + 1 + size, units, mantissa, exp, value


def _GetTimeDelta(m, index):
    return index + 2, m[index] * 256 + m[index + 1]


def _ParseMeter(m, index):
    if index + 2 > len(m):
        raise ValueError("cannot parse value")
    c1 = m[index]
    unit_extra = (c1 & 0x80) >> 7

    kind = c1 & 0x1f
    rate = (c1 & 0x60) >> 5
    c2 = m[index + 1]
    size = c2 & 0x7
    unit = (c2 & 0x18) >> 3 | unit_extra << 2
    exp = (c2 & 0xe0) >> 5
    index += 2
    out = {
        "type": kind,
        "unit": unit,
        "exp": exp,
        "rate": rate,
    }
    if index + size > len(m):
        raise ValueError("cannot parse value")
    mantissa = m[index: index + size]
    index += size
    value = _GetSignedValue(mantissa) / pow(10, exp)
    out["mantissa"], out["_value"] = mantissa, value
    if index + 2 <= len(m):
        # TODO: provide non-raw version of this
        index, out["dt"] = _GetTimeDelta(m, index)
    n = 2
    if index + size <= len(m):
        mantissa = m[index: index + size]
        value = _GetSignedValue(mantissa) / pow(10, out["exp"])
        out["mantissa%d" % n], out["_value%d" % n] = mantissa, value
        index += size
        n += 1
    return index, out


def _ParseByte(m, index):
    if len(m) <= index:
        raise ValueError("cannot parse byte")
    return index + 1, m[index]


def _ParseOptionalByte(m, index):
    if len(m) <= index:
        return index, None
    return index + 1, m[index]


def _ParseWord(m, index):
    if len(m) <= index + 1:
        raise ValueError("cannot parse word")
    return index + 2, m[index] * 256 + m[index + 1]


ENCODING_TO_DECODER = [
    "ascii",
    "latin1",  # "cp437" ,
    "utf-16-be",
]


def DecodeName(m):
    encoding = m[0] & 3
    return bytes(m[1:]).decode(ENCODING_TO_DECODER[encoding])


def _ParseName(m, index):
    assert len(m) > index
    return len(m), m[index:]


def _ParseStringWithLength(m, index):
    size = m[index]
    return 1 + size + index, bytes(m[index + 1: index + 1 + size])


def _ParseStringWithLengthAndEncoding(m, index):
    encoding = m[index] >> 5
    size = m[index] & 0x1f
    return 1 + size, {"encoding": encoding, "text": m[index + 1:index + 1 + size]}


def _ParseListRest(m, index):
    size = len(m) - index
    return index + size, m[index:index + size]


def _ParseGroups(m, index):
    if (len(m) - index) % 7 != 0:
        raise ValueError("malformed groups section: %d" % len(m))
    groups = []
    while index < len(m):
        num = m[index + 0]
        profile = m[index + 2] * 256 + m[index + 3]
        event = m[index + 5] * 256 + m[index + 6]
        groups.append((num, profile, event))
        index += 7
    return index, groups


def _ParseNonce(m, index):
    size = 8
    if len(m) < index + size:
        raise ValueError("malformed nonce:")
    return index + size, m[index:index + size]


def _GetIntLittleEndian(m):
    x = 0
    shift = 0
    for i in m:
        x += i << shift
        shift += 8
    return x


def _GetIntBigEndian(m):
    x = 0
    for i in m:
        x <<= 8
        x += i
    return x


def _ParseRestLittleEndianInt(m, index):
    size = len(m) - index
    return index + size, {"size": size, "value": _GetIntLittleEndian(m[index:index + size])}


def _ParseSizedLittleEndianInt(m, index):
    size = m[index]
    index += 1
    return index + size, {"size": size, "value": _GetIntLittleEndian(m[index:index + size])}


def _ParseOptionalTarget(m, index):
    # we need at least two bytes
    if len(m) <= index:
        return index, None
    n = m[index]
    index += 1
    if len(m) < index + 2 * n:
        raise ValueError("not enough bytes for target")
    out = []
    for _ in range(n):
        out.append(m[index] * 256 + m[index + 1])
        index += 2
    return index, out


def _ParseSensor(m, index):
    # we need at least two bytes
    if len(m) < index + 2:
        raise ValueError("malformed sensor string")

    c = m[index]
    precision = (c >> 5) & 7
    unit = (c >> 3) & 3
    size = c & 7
    if size not in (1, 2, 4):
        raise ValueError("strange size field: %d" % size)

    if len(m) < index + 1 + size:
        raise ValueError("malformed sensor string precision:%d unit:%d size:%d" %
                         (precision, unit, size))
    mantissa = m[index + 1: index + 1 + size]
    value = _GetSignedValue(mantissa) / pow(10, precision)
    return index + 1 + size, {"exp": precision, "unit": unit, "mantissa": mantissa,
                              "_value": value}


def _ParseValue(m, index):
    size = m[index] & 0x7
    start = index + 1
    return index + 1 + size, {"size": size, "value": _GetIntBigEndian(m[start:start + size])}


def _ParseDate(m, index):
    if len(m) < index + 7:
        raise ValueError("malformed time data")

    year = m[index] * 256 + m[index + 1]
    month = m[index + 2]
    day = m[index + 3]
    hours = m[index + 4]
    mins = m[index + 5]
    secs = m[index + 6]
    return index + 7, [year, month, day, hours, mins, secs]


def _ParseExtensions(m, index):
    mode = m[index]
    index += 1
    extensions = []
    unencrypted = [mode]
    has_unencypted_extension = (mode & 1) != 0
    while has_unencypted_extension:
        size = m[index]
        kind = m[index + 1]
        data = m[index + 2: index + size]
        unencrypted += m[index: index + size]
        index += size
        extensions.append((kind, data))
        has_unencypted_extension = (kind & 128) != 0
    return len(m), {"mode": mode, "extensions": extensions, "ciphertext": m[index:],
                    "_plaintext": unencrypted, "_message_size": len(m)}


# ======================================================================
# Assemble Helpers
# ======================================================================

def _MakeDate(date):
    if len(date) != 6:
        raise ValueError("bad date parameter of length %d" % len(date))
    return [date[0] // 256, date[0] % 256, date[1], date[2], date[3], date[4], date[5]]


def _MakeSensor(args):
    if '_value' in args:
        v = int(args['_value'] * pow(10, args['exp']))
        m = _SetSignedValue(v)
    else:
        m = args["mantissa"]
    c = args["exp"] << 5 | args["unit"] << 3 | len(m)
    return [c] + m


def _MakeMeter(args):
    c1 = (args["unit"] & 4) << 7 | args["rate"] << 5 | (args["type"] & 0x1f)
    c2 = args["exp"] << 5 | (args["unit"] & 3) << 3 | len(args["mantissa"])
    delta = []
    if "dt" in args:
        dt = args["dt"]
        delta = [dt >> 8, dt & 0xff]
    return [c1, c2] + args["mantissa"] + delta + args.get("mantissa2", [])


def _MakeByte(b):
    return [b]


def _MakeOptionalByte(b):
    if b is None:
        return []
    return [b]


def _MakeWord(w):
    return [(w >> 8) & 0xff, w & 0xff]


def _MakeName(n):
    return n


def _MakeList(lst):
    return lst


def _MakeNonce(lst):
    if len(lst) != 8:
        raise ValueError("bad nonce parameter of length %d" % len(lst))
    return lst


def _MakeValue(v):
    size = v["size"]
    value = v["value"]
    out = [size]
    for i in reversed(range(size)):
        out.append((value >> 8 * i) & 0xff)
    return out


def _MakeString(v):
    m = v["text"]
    c = (v["encoding"] << 5) | len(m)
    return [c] + m


def _MakeStringWithLength(v):
    return [len(v)] + [int(x) for x in v]


def _MakeLittleEndianInt(v):
    n = v["value"]
    out = []
    for _ in range(v["size"]):
        out.append(n & 0xff)
        n >>= 8
    return out


def _MakeSizedLittleEndianInt(v):
    n = v["value"]
    out = [v["size"]]
    for _ in range(v["size"]):
        out.append(n & 0xff)
        n >>= 8
    return out


def _MakeOptionalTarget(v):
    if v is None: return []
    out = [len(v)]
    for w in v:
        out.append((w >> 8) & 255)
        out.append(w & 255)
    return out


def _MakeGroups(v):
    out = []
    for num, profile, event in v:
        out.append(num)
        out.append(0)
        out.append((profile >> 8) & 255)
        out.append(profile & 255)
        out.append(0)
        out.append((event >> 8) & 255)
        out.append(event & 255)
    return out


def _MakeExtensions(v):
    out = [v["mode"]]
    for kind, data in v["extensions"]:
        out += [len(data) + 2, kind]
        out += data
    out += v["ciphertext"]
    return out


_OPTIONAL_COMPONENTS = {'b', 't'}

# Whenever you augment this make sure there is a test case in
# TestData/commands.input.txt
_PARSE_ACTIONS = {
    "A": (_ParseStringWithLength,    _MakeStringWithLength),
    "B": (_ParseByte,                _MakeByte),
    "C": (_ParseDate,                _MakeDate),
    "E": (_ParseExtensions,          _MakeExtensions),
    "F": (_ParseStringWithLengthAndEncoding, _MakeString),
    "G": (_ParseGroups,              _MakeGroups),
    "L": (_ParseListRest,            _MakeList),
    "M": (_ParseMeter,               _MakeMeter),
    "N": (_ParseName,                _MakeName),
    "O": (_ParseNonce,               _MakeNonce),
    "R": (_ParseRestLittleEndianInt, _MakeLittleEndianInt),  # as integer
    # "T": _ParseSizedLittleEndianInt,
    "V": (_ParseValue,               _MakeValue),
    "W": (_ParseWord,                _MakeWord),
    "X": (_ParseSensor,              _MakeSensor),
    # Maybes
    'b': (_ParseOptionalByte,        _MakeOptionalByte),
    't': (_ParseOptionalTarget,      _MakeOptionalTarget)
}



# not used at the moment
def MaybePatchCommand(m):
    if ((m[0], m[1]) == z.SensorMultilevel_Report and m[2] == 1 and ((m[3] & 7) > len(m) - 4)):
        x = 1 << 5 | (0 << 3) | 2
        # [49, 5, 1, 127, 1, 10] => [49, 5, 1, X, 1, 10]
        logging.error("A fixing up SensorMultilevel_Report %s: [3] %02x-> %02x", ["%02x" % i for i in m], m[3], x)
        m[3] = x

    if ((m[0], m[1]) == z.SensorMultilevel_Report and m[2] == 1 and (m[3] & 0x10) != 0):
        x = m[3] & 0xe7
        logging.error("B fixing up SensorMultilevel_Report %s: [3] %02x-> %02x", ["%02x" % i for i in m], m[3], x)
        m[3] = x

    if (m[0], m[1]) == z.Version_CommandClassReport and len(m) == 3:
        m.append(1)

    # if (m[0], m[1]) == z.SensorMultilevel_Report and (m[3] & 7) not in (1, 2, 4):
    #     size = m[3] & 7
    #     if size == 3: size = 2
    #     elif size == 7: size = 1
    #     elif size == 6: size = 2
    #     x = m[3] & 0xf8 | size
    #     logging.error("C fixing up SensorMultilevel_Report %s: [3] %02x-> %02x", Hexify(m), m[3], x)
    #     m[3] = x

    return m


class NodeCommand:

    def __init__(self, command:tuple, commandValues:dict=None, endPoint=None):
        if commandValues is None: commandValues = {}
        self.command = command
        self.commandValues = commandValues
        self.endPoint = endPoint

    def parseData(self, data):
        if len(data) < 2: return False
        self.command = (data[0], data[1])
        nodeCommandParameters = data[2:]

        if self.command == z.MultiChannel_CmdEncap:
            if len(nodeCommandParameters) < 4: return False
            self.endPoint = nodeCommandParameters[0]
            self.command = (nodeCommandParameters[2], nodeCommandParameters[3])
            nodeCommandParameters = nodeCommandParameters[4:]

        table = z.SUBCMD_TO_PARSE_TABLE.get((self.command[0] << 8) + self.command[1])
        if table is None: return False

        self.commandValues = {}
        index = 0
        for t in table:
            kind = t[0]
            name = t[2:-1]
            try:    new_index, value = _PARSE_ACTIONS[kind][0](nodeCommandParameters, index)
            except: return False
            if value is None and kind not in _OPTIONAL_COMPONENTS: return False
            self.commandValues[name] = value
            index = new_index

        return True

    @classmethod
    def fromDeviceData(cls, data):
        frame = NodeCommand(())
        return frame if frame.parseData(data) else None

    def toDeviceData(self):
        table = z.SUBCMD_TO_PARSE_TABLE[(self.command[0] << 8) + self.command[1]]
        if table is None: return None

        commandParameters = []
        for t in table:
            kind = t[0]
            name = t[2:-1]
            v = self.commandValues.get(name)
            if v is None and kind not in _OPTIONAL_COMPONENTS: return None

            try:    commandParameters += _PARSE_ACTIONS[kind][1](v)
            except: return None

        command = list(self.command)
        if self.endPoint is not None:
            command = list(z.MultiChannel_CmdEncap) + [0, self.endPoint] + command

        return command + commandParameters

    def toString(self):
        return z.SUBCMD_TO_STRING[(self.command[0] << 8) + self.command[1]] + " " + str(self.commandValues)

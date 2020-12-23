# Copyright 2016 Robert Muth <robert@muth.org>
# Copyright 2020 Gerein
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
commandQuerySets.py contains helpers for creating commands in dictionary
form. It also contains list of commands the are sent to the devices
as part of the device interviewing process.
"""
from . import zwave as z

STATIC_PROPERTY_QUERIES = [
    # Query device information that never changes (first part)
    z.SensorMultilevel_SupportedGet, z.UserCode_NumberGet, z.DoorLock_ConfigurationGet, z.DoorLockLogging_SupportedGet,
    z.Meter_SupportedGet, z.SensorAlarm_SupportedGet, z.ThermostatMode_SupportedGet, z.ThermostatSetpoint_SupportedGet,
    z.Version_Get, z.SwitchMultilevel_SupportedGet, z.MultiChannel_EndPointGet, (z.ManufacturerSpecific_DeviceSpecificGet, {"type": 0}),
    (z.ManufacturerSpecific_DeviceSpecificGet, {"type": 1}), z.TimeParameters_Get, z.SwitchAll_Get, z.Alarm_SupportedGet,
    # mostly static
    z.NodeNaming_Get, z.NodeNaming_LocationGet, z.ColorSwitch_SupportedGet,
    # zwave.AssociationCommandConfiguration_SupportedGet],
    # arguably dynamic
    z.Clock_Get, z.Firmware_MetadataGet, z.CentralScene_SupportedGet, z.Association_GroupingsGet]

# Query device information that never changes (this must be last as it's the  indicator for NodeStatus.INTERVIEWED)
STATIC_PROPERTY_QUERIES_LAST = [ (z.ManufacturerSpecific_DeviceSpecificGet, {"type": 0}), z.ManufacturerSpecific_Get, z.ZwavePlusInfo_Get]
# (z.ManufacturerSpecific_DeviceSpecificGet, {"type": 1}),

def getStaticCommands(values):
    return [(i, {}) if not isinstance(i[1], dict) else i for i in STATIC_PROPERTY_QUERIES] + \
           [(z.Version_CommandClassGet, {"class": c}) for c in values.Classes()] + \
           [(i, {}) if not isinstance(i[1], dict) else i for i in STATIC_PROPERTY_QUERIES_LAST]


DYNAMIC_PROPERTY_QUERIES = [
    # Query device information that changes frequently
    z.Basic_Get, z.Alarm_Get, z.SensorBinary_Get, z.Battery_Get, z.Lock_Get, z.DoorLock_Get,
    z.Powerlevel_Get, z.Protection_Get, z.SwitchBinary_Get, z.SwitchMultilevel_Get, z.SwitchToggleBinary_Get,
    z.Indicator_Get, (z.SceneActuatorConf_Get, {"scene": 0}), z.SensorAlarm_Get, z.ThermostatMode_Get ]
# (zwave.SensorBinary, zwave.SensorBinary_Get, {}),

def getDynamicCommands(values):
    associationQueries = []
    for no in values.AssociationGroupIds():
        associationQueries += [(z.Association_Get,                     {"group": no}),
                               (z.AssociationGroupInformation_NameGet, {"group": no}),
                               (z.AssociationGroupInformation_ListGet, {"group": no, "mode": 0}),
                               (z.AssociationGroupInformation_InfoGet, {"group": no, "mode": 0})]

    return [(i, {}) if not isinstance(i[1], dict) else i for i in DYNAMIC_PROPERTY_QUERIES] + \
           [(z.SensorMultilevel_Get, {})] + [(z.SensorMultilevel_Get, {"sensor": s}) for s in values.SensorSupported()] + \
           [(z.Meter_Get, {})] + [(z.Meter_Get, {"scale": s << 3}) for s in values.MeterSupported()] + \
           [(z.ColorSwitch_Get, {"group": g}) for g in values.ColorSwitchSupported()] + \
           associationQueries + \
           [(z.MultiChannel_CapabilityGet, {"endpoint": e}) for e in values.MultiChannelEndPointIds()]


def CommandVersionQueries(classes):         return [(z.Version_CommandClassGet, {"class": c}) for c in classes]
def SceneActuatorConfiguration(scenes):     return [(z.SceneActuatorConf_Get, {"scene": s}) for s in scenes]
def ParameterQueries(params):               return [(z.Configuration_Get, {"parameter": p}) for p in params]
def AssociationAdd(group, n):               return [(z.Association_Set, {"group": group, "nodes": [n]}), (z.Association_Get, {"group": group})]
def AssociationRemove(group, n):            return [(z.Association_Remove, {"group": n, "nodes": [n]}), (z.Association_Get, {"group": group})]

# Version 1 of the command class does not support `delay`
def MultilevelSwitchSet(val, delay=0, request_update=True):  return [(z.SwitchMultilevel_Set, {"level": val, "duration": delay})] + ([(z.SwitchBinary_Get, {}), (z.SwitchMultilevel_Get, {})] if request_update else [])
def BinarySwitchSet(val, request_update=True):               return [(z.SwitchBinary_Set, {"level": val})] + ([(z.SwitchBinary_Get, {}), (z.SwitchMultilevel_Get, {})] if request_update else [])
def ConfigurationSet(param, size, val, request_update=True): return [(z.Configuration_Set, {"parameter": param, "value": {"size": size, "value": val}})] + ([(z.Configuration_Get, {"parameter": param})] if request_update else [])
def BasicSet(val, request_update=True):                      return [(z.Basic_Set, {"level": val})] + ([(z.Basic_Get, {})] if request_update else [])
def ResetMeter(_request_update=True):                        return [(z.Meter_Reset, {})] # + ([(z.Meter_Get, {})] if not request_update else []) # TODO
def SceneActuatorConfSet(scene, delay, extra, level, request_update=True): return [(z.SceneActuatorConf_Set, {"scene": scene, "delay": delay, "extra": extra, "level": level})] + ([(z.SceneActuatorConf_Get, {"scene": scene})] if request_update else [])

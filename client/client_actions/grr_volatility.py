#!/usr/bin/env python
# Copyright 2012 Google Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Execute a volatility command on the client memory.

This module implements the volatility enabled client actions which enable
volatility to operate directly on the client.
"""


import sys


from volatility import obj
from volatility import plugins
from volatility import session
from volatility.plugins.addrspaces import standard
from volatility.ui import renderer

from grr.client import actions
from grr.client import vfs
from grr.lib import utils
from grr.proto import jobs_pb2

# We use a global session for memory analysis of the live system.
SESSION_CACHE = utils.FastStore()


class ProtobufRenderer(renderer.RendererBaseClass):
  """This stores all the data in a protobuf."""

  class Modes(object):
    TABLE = 1
    STRING = 2

  def __init__(self):
    self.response = jobs_pb2.VolatilityResponse()
    self.active_section = None
    self.mode = None

  def InitSection(self, mode=None):
    if self.mode != mode:
      self.active_section = None
    if not self.active_section:
      self.active_section = self.response.sections.add()
    self.mode = mode

  def end(self):
    pass

  def start(self, plugin_name=None):
    if plugin_name:
      self.response.plugin = plugin_name

  def write(self, data):
    self.format(data)

  def format(self, formatstring, *data):
    _ = formatstring, data

    self.InitSection(self.Modes.STRING)
    active_list = self.active_section.formatted_value_list
    formatted_value = active_list.formatted_values.add()
    formatted_value.formatstring = formatstring
    values = formatted_value.data

    for d in data:
      self.AddValue(values, d)

  def section(self):
    self.active_section = None

  def flush(self):
    pass

  def table_header(self, title_format_list=None, suppress_headers=False,
                   name=None):
    _ = suppress_headers, name

    self.InitSection(self.Modes.TABLE)

    for (print_name, name, format_hint) in title_format_list:
      header_pb = self.active_section.table.headers.add()
      header_pb.print_name = print_name
      header_pb.name = name
      header_pb.format_hint = format_hint

  def AddValue(self, row, value):

    response = row.values.add()
    if isinstance(value, obj.BaseObject):
      response.type = value.obj_type
      response.name = value.obj_name
      response.offset = value.obj_offset
      response.vm = utils.SmartStr(value.obj_vm)

      try:
        response.value = value.__int__()
      except (AttributeError, ValueError):
        pass

      try:
        string_value = value.__unicode__()
      except (AttributeError, ValueError):
        try:
          string_value = value.__str__()
        except (AttributeError, ValueError):
          pass

      if string_value:
        try:
          int_value = int(string_value)
          # If the string converts to an int but to a different one as the int
          # representation, we send it.
          if int_value != response.value:
            response.svalue = utils.SmartUnicode(string_value)
        except ValueError:
          # We also send if it doesn't convert back to an int.
          response.svalue = utils.SmartUnicode(string_value)

    elif isinstance(value, (bool)):
      response.svalue = utils.SmartUnicode(str(value))
    elif isinstance(value, (int, long)):
      response.value = value
    elif isinstance(value, (basestring)):
      response.svalue = utils.SmartUnicode(value)
    elif isinstance(value, obj.NoneObject):
      response.type = value.__class__.__name__
      response.reason = value.reason
    else:
      response.svalue = utils.SmartUnicode(repr(value))

  def table_row(self, *args):
    """Outputs a single row of a table."""

    self.InitSection(self.Modes.TABLE)

    row = self.active_section.table.rows.add()
    for value in args:
      self.AddValue(row, value)

  def GetResponse(self):
    return self.response


class UnicodeStringIO(object):
  """Just like StringIO but uses unicode strings."""

  def __init__(self):
    self.data = u""

  def write(self, data):
    self.data += utils.SmartUnicode(data)

  def getvalue(self):
    return self.data


class VolatilityAction(actions.ActionPlugin):
  """Runs a volatility command on live memory."""
  in_protobuf = jobs_pb2.VolatilityRequest
  out_protobuf = jobs_pb2.VolatilityResponse

  def GuessProfile(self, vol_session):
    """Guesses a likely profile from the client."""
    if sys.platform.startswith("win"):
      # Have volatility itself guess the profile.
      return vol_session.plugins.guess_profile(session=vol_session)

  def Run(self, args):
    """Run a volatility plugin and return the result."""
    # Recover the volatility session.
    try:
      vol_session = SESSION_CACHE.Get(args.device.path)
    except KeyError:

      def Progress(message=None, **_):
        """Allow volatility to heartbeat us so we do not die."""
        _ = message
        self.Progress()

      vol_session = session.Session()
      vol_session.fhandle = vfs.VFSOpen(args.device)

      # Install the heartbeat mechanism.
      vol_session.progress = Progress

      # Get the dtb from the driver if possible.
      try:
        vol_session.dtb = vol_session.fhandle.cr3
      except AttributeError:
        pass

      try:
        vol_session.profile = self.GuessProfile(vol_session)
      except AttributeError:
        # TODO(user): Volatility does not have this plugin yet.
        pass

      # Have a default if we cant guess.
      if not vol_session.profile:
        vol_session.profile = "Win7SP1x64"

      SESSION_CACHE.Put(args.device.path, vol_session)

    vol_args = utils.ProtoDict(args.args)
    for k, v in vol_args.ToDict().items():
      setattr(vol_session, k, v)

    for plugin in args.plugins:
      error = ""

      # Heartbeat the client to ensure we keep our nanny happy.
      vol_session.progress(message="Running plugin %s" % plugin)

      result_renderer = ProtobufRenderer()
      result_renderer.start(plugin)
      try:
        # Get the plugin the server asked for.
        vol_plugin = getattr(vol_session.plugins, plugin)(session=vol_session)

        # Render the results.
        vol_plugin.render(result_renderer)

      # Whatever happens here we need to report it.
      except Exception as e:
        error = str(e)

      response = result_renderer.GetResponse()
      if error:
        response.error = error

      # Send it back to the server.
      self.SendReply(response)
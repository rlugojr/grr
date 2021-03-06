#!/usr/bin/env python
"""Helper functionality for gui testing."""

import functools
import os
import time
import urlparse

# We have to import test_lib first to properly initialize aff4 and rdfvalues.
# pylint: disable=g-bad-import-order
from grr.lib import test_lib
# pylint: enable=g-bad-import-order

from selenium.common import exceptions
from selenium.webdriver.common import action_chains
from selenium.webdriver.common import keys
from selenium.webdriver.support import select

import logging

from grr.gui import api_auth_manager
from grr.gui import api_call_router_with_approval_checks
from grr.gui import webauth

from grr.lib import access_control
from grr.lib import action_mocks
from grr.lib import aff4
from grr.lib import artifact_registry
from grr.lib import client_index
from grr.lib import data_store
from grr.lib import flow
from grr.lib import hunts
from grr.lib import rdfvalue
from grr.lib import utils
from grr.lib.aff4_objects import aff4_grr
from grr.lib.aff4_objects import standard as aff4_standard
from grr.lib.aff4_objects import user_managers
from grr.lib.aff4_objects import users
from grr.lib.flows.general import transfer
from grr.lib.hunts import results as hunts_results
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import crypto as rdf_crypto
from grr.lib.rdfvalues import flows as rdf_flows
from grr.lib.rdfvalues import paths as rdf_paths
from grr.lib.rdfvalues import structs as rdf_structs
from grr.proto import tests_pb2
from grr.server import foreman as rdf_foreman

# A increasing sequence of times.
TIME_0 = test_lib.FIXTURE_TIME
TIME_1 = TIME_0 + rdfvalue.Duration("1d")
TIME_2 = TIME_1 + rdfvalue.Duration("1d")


def DateString(t):
  return t.Format("%Y-%m-%d")


def DateTimeString(t):
  return t.Format("%Y-%m-%d %H:%M:%S")


def CreateFileVersions(token):
  """Add new versions for a file."""
  # This file already exists in the fixture at TIME_0, we write a
  # later version.
  CreateFileVersion(
      "aff4:/C.0000000000000001/fs/os/c/Downloads/a.txt",
      "Hello World",
      timestamp=TIME_1,
      token=token)
  CreateFileVersion(
      "aff4:/C.0000000000000001/fs/os/c/Downloads/a.txt",
      "Goodbye World",
      timestamp=TIME_2,
      token=token)


def CreateFileVersion(path, content, timestamp, token=None):
  """Add a new version for a file."""
  with test_lib.FakeTime(timestamp):
    with aff4.FACTORY.Create(
        path, aff4_type=aff4_grr.VFSFile, mode="w", token=token) as fd:
      fd.Write(content)
      fd.Set(fd.Schema.CONTENT_LAST, rdfvalue.RDFDatetime.Now())


def CreateFolder(path, timestamp, token=None):
  """Creates a VFS folder."""
  with test_lib.FakeTime(timestamp):
    with aff4.FACTORY.Create(
        path, aff4_type=aff4_standard.VFSDirectory, mode="w", token=token) as _:
      pass


def SeleniumAction(f):
  """Decorator to do multiple attempts in case of WebDriverException."""

  @functools.wraps(f)
  def Decorator(*args, **kwargs):
    delay = 0.2
    num_attempts = 15
    cur_attempt = 0
    while True:
      try:
        return f(*args, **kwargs)
      except exceptions.WebDriverException as e:
        logging.warn("Selenium raised %s", utils.SmartUnicode(e))

        cur_attempt += 1
        if cur_attempt == num_attempts:
          raise

        time.sleep(delay)

  return Decorator


class ACLChecksEnabledContextManager(object):
  """Enable ACL Checks."""

  def __enter__(self):
    self.Start()

  def Start(self):
    self.old_security_manager = data_store.DB.security_manager
    data_store.DB.security_manager = user_managers.FullAccessControlManager()

  def __exit__(self, unused_type, unused_value, unused_traceback):
    self.Stop()

  def Stop(self):
    data_store.DB.security_manager = self.old_security_manager


class GRRSeleniumTest(test_lib.GRRBaseTest):
  """Baseclass for selenium UI tests."""

  # Default duration (in seconds) for WaitUntil.
  duration = 5

  # Time to wait between polls for WaitUntil.
  sleep_time = 0.2

  # This is the global selenium handle.
  driver = None

  # Base url of the Admin UI
  base_url = None

  # Also indicates whether InstallACLChecks() was called during the test.
  acl_manager = None

  def InstallACLChecks(self):
    """Installs AccessControlManager and stubs out SendEmail."""
    # Clear the cache of the approvals-based router.
    (api_call_router_with_approval_checks.
     ApiCallRouterWithApprovalChecksWithRobotAccess).ClearCache()

    if self.acl_manager:
      return

    self.acl_manager = ACLChecksEnabledContextManager()
    self.acl_manager.Start()

    acrwac = api_call_router_with_approval_checks
    name = acrwac.ApiCallRouterWithApprovalChecksWithRobotAccess.__name__
    self.config_override = test_lib.ConfigOverrider({"API.DefaultRouter": name})
    self.config_override.Start()
    # Make sure ApiAuthManager is initialized with this configuration setting.
    api_auth_manager.APIACLInit.InitApiAuthManager()

  def UninstallACLChecks(self):
    """Deinstall previously installed ACL checks."""
    if not self.acl_manager:
      return

    self.acl_manager.Stop()
    self.acl_manager = None

    self.config_override.Stop()
    self.config_override = None

    # Make sure ApiAuthManager is initialized with update configuration
    # setting (i.e. without overrides).
    api_auth_manager.APIACLInit.InitApiAuthManager()

  def ACLChecksDisabled(self):
    return test_lib.ACLChecksDisabledContextManager()

  def CheckJavascriptErrors(self):
    for message in self.driver.get_log("browser"):
      if (message.get("source", "") == "javascript" and
          message.get("level", "") == "SEVERE"):
        self.fail("Javascript error ecountered during test: %s" %
                  message["message"])

  def WaitUntil(self, condition_cb, *args):
    self.CheckJavascriptErrors()

    for _ in xrange(int(self.duration / self.sleep_time)):
      try:
        res = condition_cb(*args)
        if res:
          return res

      # The element might not exist yet and selenium could raise here. (Also
      # Selenium raises Exception not StandardError).
      except Exception as e:  # pylint: disable=broad-except
        logging.warn("Selenium raised %s", utils.SmartUnicode(e))

      time.sleep(self.sleep_time)

    raise RuntimeError("condition not met, body is: %s" %
                       self.driver.find_element_by_tag_name("body").text)

  def _FindElement(self, selector):
    try:
      selector_type, effective_selector = selector.split("=", 1)
    except ValueError:
      effective_selector = selector
      selector_type = None

    if selector_type == "css":
      elems = self.driver.execute_script(
          "return $(\"" + effective_selector.replace("\"", "\\\"") + "\");")
      elems = [e for e in elems if e.is_displayed()]

      if not elems:
        raise exceptions.NoSuchElementException()
      else:
        return elems[0]

    elif selector_type == "link":
      links = self.driver.find_elements_by_partial_link_text(effective_selector)
      for l in links:
        if l.text.strip() == effective_selector:
          return l
      raise exceptions.NoSuchElementException()

    elif selector_type == "xpath":
      return self.driver.find_element_by_xpath(effective_selector)

    elif selector_type == "id":
      return self.driver.find_element_by_id(effective_selector)

    elif selector_type == "name":
      return self.driver.find_element_by_name(effective_selector)

    elif selector_type is None:
      if effective_selector.startswith("//"):
        return self.driver.find_element_by_xpath(effective_selector)
      else:
        return self.driver.find_element_by_id(effective_selector)
    else:
      raise RuntimeError("unknown selector type %s" % selector_type)

  @SeleniumAction
  def Open(self, url):
    self.driver.get(self.base_url + url)

    # Sometimes page doesn't get refreshed if url's path and query haven't
    # changed, even if fragments part (part after '#' symbol) of the url has
    # changed. We have to explicitly call Refresh() in such cases.
    prev_parsed_url = urlparse.urlparse(self.driver.current_url)
    new_parsed_url = urlparse.urlparse(url)
    if (prev_parsed_url.path == new_parsed_url.path and
        prev_parsed_url.query == new_parsed_url.query):
      self.Refresh()

  @SeleniumAction
  def Refresh(self):
    self.driver.refresh()

  @SeleniumAction
  def Back(self):
    self.driver.back()

  @SeleniumAction
  def Forward(self):
    self.driver.forward()

  def WaitUntilNot(self, condition_cb, *args):
    self.WaitUntil(lambda: not condition_cb(*args))

  def GetPageTitle(self):
    return self.driver.title

  def IsElementPresent(self, target):
    try:
      self._FindElement(target)
      return True
    except exceptions.NoSuchElementException:
      return False

  def GetCurrentUrlPath(self):
    url = urlparse.urlparse(self.driver.current_url)

    result = url.path
    if url.fragment:
      result += "#" + url.fragment

    return result

  def GetElement(self, target):
    try:
      return self._FindElement(target)
    except exceptions.NoSuchElementException:
      return None

  def GetVisibleElement(self, target):
    try:
      element = self._FindElement(target)
      if element.is_displayed():
        return element
    except exceptions.NoSuchElementException:
      pass

    return None

  def IsTextPresent(self, text):
    return self.AllTextsPresent([text])

  def AllTextsPresent(self, texts):
    body = self.driver.find_element_by_tag_name("body").text
    for text in texts:
      if utils.SmartUnicode(text) not in body:
        return False
    return True

  def IsVisible(self, target):
    element = self.GetElement(target)
    return element and element.is_displayed()

  def GetText(self, target):
    element = self.WaitUntil(self.GetVisibleElement, target)
    return element.text.strip()

  def GetValue(self, target):
    return self.GetAttribute(target, "value")

  def GetAttribute(self, target, attribute):
    element = self.WaitUntil(self.GetVisibleElement, target)
    return element.get_attribute(attribute)

  def IsUserNotificationPresent(self, contains_string):
    self.Click("css=#notification_button")
    self.WaitUntil(self.IsElementPresent, "css=grr-user-notification-dialog")
    self.WaitUntilNot(self.IsElementPresent,
                      "css=grr-user-notification-dialog:contains('Loading...')")

    notifications_text = self.GetText("css=grr-user-notification-dialog")
    self.Click("css=grr-user-notification-dialog button:contains('Close')")

    return contains_string in notifications_text

  def GetJavaScriptValue(self, js_expression):
    return self.driver.execute_script(js_expression)

  def _WaitForAjaxCompleted(self):
    self.WaitUntilEqual(
        0, self.GetJavaScriptValue,
        "return $('#ajax_spinner').scope().controller.queue.length")

  @SeleniumAction
  def Type(self, target, text, end_with_enter=False):
    element = self.WaitUntil(self.GetVisibleElement, target)
    element.clear()
    element.send_keys(text)
    if end_with_enter:
      element.send_keys(keys.Keys.ENTER)

    # We experienced that Selenium sometimes swallows the last character of the
    # text sent. Raising an exception here will just retry in that case.
    if not end_with_enter:
      if text != self.GetValue(target):
        raise exceptions.WebDriverException("Send_keys did not work correctly.")

  @SeleniumAction
  def Click(self, target):
    # Selenium clicks elements by obtaining their position and then issuing a
    # click action in the middle of this area. This may lead to misclicks when
    # elements are moving. Make sure that they are stationary before issuing
    # the click action (specifically, using the bootstrap "fade" class that
    # slides dialogs in is highly discouraged in combination with .Click()).

    # Since Selenium does not know when the page is ready after AJAX calls, we
    # need to wait for AJAX completion here to be sure that all event handlers
    # are attached to their respective DOM elements.
    self._WaitForAjaxCompleted()

    element = self.WaitUntil(self.GetVisibleElement, target)
    element.click()

  @SeleniumAction
  def DoubleClick(self, target):
    # Selenium clicks elements by obtaining their position and then issuing a
    # click action in the middle of this area. This may lead to misclicks when
    # elements are moving. Make sure that they are stationary before issuing
    # the click action (specifically, using the bootstrap "fade" class that
    # slides dialogs in is highly discouraged in combination with
    # .DoubleClick()).

    # Since Selenium does not know when the page is ready after AJAX calls, we
    # need to wait for AJAX completion here to be sure that all event handlers
    # are attached to their respective DOM elements.
    self._WaitForAjaxCompleted()

    element = self.WaitUntil(self.GetVisibleElement, target)
    action_chains.ActionChains(self.driver).double_click(element).perform()

  @SeleniumAction
  def Select(self, target, label):
    element = self.WaitUntil(self.GetVisibleElement, target)
    select.Select(element).select_by_visible_text(label)

  def GetSelectedLabel(self, target):
    element = self.WaitUntil(self.GetVisibleElement, target)
    return select.Select(element).first_selected_option.text.strip()

  def IsChecked(self, target):
    return self.WaitUntil(self.GetVisibleElement, target).is_selected()

  def GetCssCount(self, target):
    if not target.startswith("css="):
      raise ValueError("invalid target for GetCssCount: " + target)

    return len(self.driver.find_elements_by_css_selector(target[4:]))

  def WaitUntilEqual(self, target, condition_cb, *args):
    for _ in xrange(int(self.duration / self.sleep_time)):
      try:
        if condition_cb(*args) == target:
          return True

      # The element might not exist yet and selenium could raise here. (Also
      # Selenium raises Exception not StandardError).
      except Exception as e:  # pylint: disable=broad-except
        logging.warn("Selenium raised %s", utils.SmartUnicode(e))

      time.sleep(self.sleep_time)

    raise RuntimeError("condition not met, body is: %s" %
                       self.driver.find_element_by_tag_name("body").text)

  def WaitUntilContains(self, target, condition_cb, *args):
    data = ""
    target = utils.SmartUnicode(target)

    for _ in xrange(int(self.duration / self.sleep_time)):
      try:
        data = condition_cb(*args)
        if target in data:
          return True

      # The element might not exist yet and selenium could raise here.
      except Exception as e:  # pylint: disable=broad-except
        logging.warn("Selenium raised %s", utils.SmartUnicode(e))

      time.sleep(self.sleep_time)

    raise RuntimeError("condition not met. Got %r" % data)

  def _MakeFixtures(self):
    # Install the mock security manager so we can trap errors in interactive
    # mode.
    data_store.DB.security_manager = test_lib.MockSecurityManager()
    token = access_control.ACLToken(username="test", reason="Make fixtures.")
    token = token.SetUID()

    for i in range(10):
      client_id = rdf_client.ClientURN("C.%016X" % i)
      with aff4.FACTORY.Create(
          client_id, aff4_grr.VFSGRRClient, mode="rw",
          token=token) as client_obj:
        index = client_index.CreateClientIndex(token=token)
        index.AddClient(client_obj)

  def setUp(self):
    super(GRRSeleniumTest, self).setUp()

    self.token.username = "gui_user"
    webauth.WEBAUTH_MANAGER.SetUserName(self.token.username)

    # Make the user use the advanced gui so we can test it.
    with aff4.FACTORY.Create(
        aff4.ROOT_URN.Add("users/%s" % self.token.username),
        aff4_type=users.GRRUser,
        mode="w",
        token=self.token) as user_fd:
      user_fd.Set(user_fd.Schema.GUI_SETTINGS(mode="ADVANCED"))

    self._MakeFixtures()

    # Clean artifacts sources.
    artifact_registry.REGISTRY.ClearSources()
    artifact_registry.REGISTRY.AddDatastoreSources(
        [aff4.ROOT_URN.Add("artifact_store")])

    self.InstallACLChecks()

  def tearDown(self):
    self.UninstallACLChecks()
    super(GRRSeleniumTest, self).tearDown()

  def DoAfterTestCheck(self):
    super(GRRSeleniumTest, self).DoAfterTestCheck()
    self.CheckJavascriptErrors()

  def WaitForNotification(self, user):
    sleep_time = 0.2
    iterations = 50
    for _ in xrange(iterations):
      try:
        fd = aff4.FACTORY.Open(user, users.GRRUser, mode="r", token=self.token)
        pending_notifications = fd.Get(fd.Schema.PENDING_NOTIFICATIONS)
        if pending_notifications:
          return
      except IOError:
        pass
      time.sleep(sleep_time)
    self.fail("Notification for user %s never sent." % user)


class GRRSeleniumHuntTest(GRRSeleniumTest):
  """Common functionality for hunt gui tests."""

  def _CreateHuntWithDownloadedFile(self):
    with self.ACLChecksDisabled():
      hunt = self.CreateSampleHunt(
          path=os.path.join(self.base_path, "test.plist"), client_count=1)

      action_mock = action_mocks.FileFinderClientMock()
      test_lib.TestHuntHelper(action_mock, self.client_ids, False, self.token)

      return hunt

  def CheckState(self, state):
    self.WaitUntil(self.IsElementPresent, "css=div[state=\"%s\"]" % state)

  def CreateSampleHunt(self,
                       path=None,
                       stopped=False,
                       output_plugins=None,
                       client_limit=0,
                       client_count=10,
                       token=None):
    token = token or self.token
    self.client_ids = self.SetupClients(client_count)

    client_rule_set = rdf_foreman.ForemanClientRuleSet(rules=[
        rdf_foreman.ForemanClientRule(
            rule_type=rdf_foreman.ForemanClientRule.Type.REGEX,
            regex=rdf_foreman.ForemanRegexClientRule(
                attribute_name="GRR client", attribute_regex="GRR"))
    ])

    with hunts.GRRHunt.StartHunt(
        hunt_name="GenericHunt",
        flow_runner_args=rdf_flows.FlowRunnerArgs(flow_name="GetFile"),
        flow_args=transfer.GetFileArgs(pathspec=rdf_paths.PathSpec(
            path=path or "/tmp/evil.txt",
            pathtype=rdf_paths.PathSpec.PathType.OS,)),
        client_rule_set=client_rule_set,
        output_plugins=output_plugins or [],
        client_rate=0,
        client_limit=client_limit,
        token=token) as hunt:
      if not stopped:
        hunt.Run()

    with aff4.FACTORY.Open("aff4:/foreman", mode="rw", token=token) as foreman:

      for client_id in self.client_ids:
        foreman.AssignTasksToClient(client_id)

    self.hunt_urn = hunt.urn
    return aff4.FACTORY.Open(
        hunt.urn, mode="rw", token=token, age=aff4.ALL_TIMES)

  def CreateGenericHuntWithCollection(self, values=None):
    self.client_ids = self.SetupClients(10)

    if values is None:
      values = [
          rdfvalue.RDFURN("aff4:/sample/1"),
          rdfvalue.RDFURN("aff4:/C.0000000000000001/fs/os/c/bin/bash"),
          rdfvalue.RDFURN("aff4:/sample/3")
      ]

    client_rule_set = rdf_foreman.ForemanClientRuleSet(rules=[
        rdf_foreman.ForemanClientRule(
            rule_type=rdf_foreman.ForemanClientRule.Type.REGEX,
            regex=rdf_foreman.ForemanRegexClientRule(
                attribute_name="GRR client", attribute_regex="GRR"))
    ])

    with hunts.GRRHunt.StartHunt(
        hunt_name="GenericHunt",
        client_rule_set=client_rule_set,
        output_plugins=[],
        token=self.token) as hunt:

      runner = hunt.GetRunner()
      runner.Start()

      with aff4.FACTORY.Open(
          hunt.results_collection_urn,
          aff4_type=hunts_results.HuntResultCollection,
          mode="w",
          token=self.token) as collection:

        for value in values:
          collection.Add(
              rdf_flows.GrrMessage(
                  payload=value, source=self.client_ids[0]))

      return hunt.urn


class SearchClientTestBase(GRRSeleniumTest):

  def CreateSampleHunt(self, description, token=None):
    return hunts.GRRHunt.StartHunt(
        hunt_name="GenericHunt", description=description, token=token)


class CanaryModeOverrider(object):
  """A context to temporarily change the canary mode flag of the user."""

  def __init__(self, token, target_canary_mode=True):
    self.token = token
    self.target_canary_mode = target_canary_mode

  def Start(self):
    with aff4.FACTORY.Create(
        aff4.ROOT_URN.Add("users").Add(self.token.username),
        aff4_type=users.GRRUser,
        mode="rw",
        token=self.token) as user:
      # Save original canary mode to reset it later.
      self.original_canary_mode = user.Get(user.Schema.GUI_SETTINGS).canary_mode

      # Set new canary mode.
      user.Set(user.Schema.GUI_SETTINGS(canary_mode=self.target_canary_mode))

  def Stop(self):
    with aff4.FACTORY.Create(
        aff4.ROOT_URN.Add("users").Add(self.token.username),
        aff4_type=users.GRRUser,
        mode="w",
        token=self.token) as user:
      # Reset canary mode to original value.
      user.Set(user.Schema.GUI_SETTINGS(canary_mode=self.original_canary_mode))


class RecursiveTestFlowArgs(rdf_structs.RDFProtoStruct):
  protobuf = tests_pb2.RecursiveTestFlowArgs


class RecursiveTestFlow(flow.GRRFlow):
  """A test flow which starts some subflows."""
  args_type = RecursiveTestFlowArgs

  # If a flow doesn't have a category, it can't be started/terminated by a
  # non-supervisor user when FullAccessControlManager is used.
  category = "/Test/"

  @flow.StateHandler()
  def Start(self):
    if self.args.depth < 2:
      for i in range(2):
        self.Log("Subflow call %d", i)
        self.CallFlow(
            RecursiveTestFlow.__name__,
            depth=self.args.depth + 1,
            next_state="End")


class FlowWithOneLogStatement(flow.GRRFlow):
  """Flow that logs a single statement."""

  @flow.StateHandler()
  def Start(self):
    self.Log("I do log.")


class FlowWithOneStatEntryResult(flow.GRRFlow):
  """Test flow that calls SendReply once with a StatEntry value."""

  @flow.StateHandler()
  def Start(self):
    self.SendReply(
        rdf_client.StatEntry(pathspec=rdf_paths.PathSpec(
            path="/some/unique/path", pathtype=rdf_paths.PathSpec.PathType.OS)))


class FlowWithOneNetworkConnectionResult(flow.GRRFlow):
  """Test flow that calls SendReply once with a NetworkConnection value."""

  @flow.StateHandler()
  def Start(self):
    self.SendReply(rdf_client.NetworkConnection(pid=42))


class FlowWithOneHashEntryResult(flow.GRRFlow):
  """Test flow that calls SendReply once with a HashEntry value."""

  @flow.StateHandler()
  def Start(self):
    hash_result = rdf_crypto.Hash(
        sha256=("9e8dc93e150021bb4752029ebbff51394aa36f069cf19901578"
                "e4f06017acdb5").decode("hex"),
        sha1="6dd6bee591dfcb6d75eb705405302c3eab65e21a".decode("hex"),
        md5="8b0a15eefe63fd41f8dc9dee01c5cf9a".decode("hex"))
    self.SendReply(hash_result)

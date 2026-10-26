"""Reviewable current-user Task Scheduler definition; never registers a task."""

import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .errors import ReviveError
from .paths import check_plain_path, within

NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def task_xml(config_file, executable, user_sid):
    if not re.fullmatch(r"S-1-[0-9-]+", user_sid):
        raise ReviveError("STARTUP_INVALID", "Expected a Windows user SID.")
    ET.register_namespace("", NAMESPACE)

    def element(parent, tag, value=None, **attributes):
        child = ET.SubElement(parent, f"{{{NAMESPACE}}}{tag}", attributes)
        if value is not None:
            child.text = value
        return child

    root = ET.Element(f"{{{NAMESPACE}}}Task", {"version": "1.3"})
    registration = element(root, "RegistrationInfo")
    element(
        registration,
        "Description",
        "Revive local photo monitor; current user, no elevation.",
    )
    triggers = element(root, "Triggers")
    trigger = element(triggers, "LogonTrigger")
    element(trigger, "Enabled", "true")
    element(trigger, "UserId", user_sid)
    element(trigger, "Delay", "PT15S")
    principals = element(root, "Principals")
    principal = element(principals, "Principal", id="User")
    element(principal, "UserId", user_sid)
    element(principal, "LogonType", "InteractiveToken")
    element(principal, "RunLevel", "LeastPrivilege")
    settings = element(root, "Settings")
    for tag, value in {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "AllowHardTerminate": "true",
        "StartWhenAvailable": "true",
        "RunOnlyIfNetworkAvailable": "false",
        "Enabled": "true",
        "Hidden": "true",
        "ExecutionTimeLimit": "PT0S",
        "WakeToRun": "false",
    }.items():
        element(settings, tag, value)
    retry = element(settings, "RestartOnFailure")
    element(retry, "Interval", "PT1M")
    element(retry, "Count", "3")
    actions = element(root, "Actions", Context="User")
    action = element(actions, "Exec")
    element(action, "Command", str(executable))
    element(
        action,
        "Arguments",
        subprocess.list2cmdline(["-m", "revive", "--config", str(config_file), "run"]),
    )
    element(action, "WorkingDirectory", str(executable.parent))
    # Match Windows' native task export: BOM-marked UTF-16 works both as a file
    # and when read into the scheduler's Unicode XmlText COM property.
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def export_startup(config, config_file):
    if os.name != "nt":
        raise ReviveError(
            "BACKEND_UNSUPPORTED",
            "Login startup export targets Windows Task Scheduler.",
        )
    executable = Path(sys.executable).with_name("pythonw.exe")
    check_plain_path(executable)
    if not executable.is_file():
        raise ReviveError(
            "STARTUP_INVALID",
            "The current environment has no windowless Python interpreter.",
        )
    whoami = (
        Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "whoami.exe"
    )
    result = subprocess.run(
        [str(whoami), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        check=False,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    match = re.search(rb"\bS-1-[0-9-]+\b", result.stdout)
    if result.returncode or not match:
        raise ReviveError(
            "STARTUP_INVALID", "Cannot determine the current Windows user SID."
        )
    config.app_data.mkdir(parents=True, exist_ok=True)
    target = within(config.app_data, config.app_data / "startup-task.xml")
    # Export is a local, reviewable artifact. No task registration or registry edits.
    data = task_xml(config_file.resolve(), executable, match[0].decode("ascii"))
    fd, filename = tempfile.mkstemp(
        prefix=".startup-", suffix=".xml", dir=config.app_data
    )
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "definition": str(target),
        "registered": False,
        "logon_type": "current interactive user",
        "elevated": False,
    }

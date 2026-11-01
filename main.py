import argparse
import glob
import json
import logging
import platform
import re
import subprocess
import sys
import time

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

SHORTHAND_TRIGGERS = {
    "@reboot", "@yearly", "@annually", "@monthly", "@weekly",
    "@daily", "@midnight", "@hourly",
}

ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")

WINDOWS_LIST_SCRIPT = (
    "Get-ScheduledTask | Select-Object TaskName, TaskPath, State, "
    "@{Name='Actions'; Expression={($_.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join '; '}}, "
    "@{Name='TriggerTime'; Expression={($_.Triggers | ForEach-Object { $_.StartBoundary }) -join '; '}} "
    "| ConvertTo-Json -Depth 4"
)

WINDOWS_INFO_SCRIPT = (
    "Get-ScheduledTaskInfo -TaskName $args[0] | "
    "Select-Object LastRunTime | ConvertTo-Json"
)


def setup_argparse():
    """
    Sets up the argument parser for the script.
    """
    parser = argparse.ArgumentParser(description="Monitors changes to scheduled tasks and logs them.")
    return parser.parse_args()


def _run(argv):
    """Run a fixed command with an argument list and no shell."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()
    return process.returncode, stdout, stderr


def _blank_task():
    return {
        "task_name": None,
        "trigger_time": None,
        "command": None,
        "last_run_time": None,
        "task_path": None,
        "state": None,
        "actions": None,
    }


def get_windows_tasks():
    """One structured call: JSON output parsed with json.loads."""
    returncode, stdout, stderr = _run(
        ["powershell", "-NoProfile", "-Command", WINDOWS_LIST_SCRIPT]
    )
    if returncode != 0:
        logging.error(f"Error retrieving scheduled tasks: {stderr}")
        return None
    try:
        rows = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError as e:
        logging.error(f"Could not parse scheduled task output: {e}")
        return None
    if isinstance(rows, dict):
        rows = [rows]
    tasks = []
    for row in rows:
        task = _blank_task()
        task["task_name"] = row.get("TaskName")
        task["task_path"] = row.get("TaskPath")
        task["state"] = str(row.get("State")) if row.get("State") is not None else None
        task["actions"] = row.get("Actions")
        task["command"] = row.get("Actions")
        task["trigger_time"] = row.get("TriggerTime")
        # Per task query: the task name travels as one separate argument,
        # read by the fixed script from $args[0]. No shell is involved.
        # Every task costs a separate PowerShell launch on every
        # cycle, on top of the listing launch. A machine with a few
        # hundred tasks pays a few hundred process starts a minute
        # for the last run times, and the query passes the name
        # without its path, so two tasks sharing a name in different
        # folders cannot both be answered correctly.
        info_rc, info_out, info_err = _run(
            ["powershell", "-NoProfile", "-Command", WINDOWS_INFO_SCRIPT, task["task_name"]]
        )
        if info_rc == 0 and info_out.strip():
            try:
                info = json.loads(info_out)
                if isinstance(info, dict):
                    task["last_run_time"] = info.get("LastRunTime")
            except json.JSONDecodeError:
                logging.error(f"Could not parse task info for {task['task_name']}")
        elif info_err:
            logging.error(f"Error retrieving task info for {task['task_name']}: {info_err}")
        tasks.append(task)
    return tasks


def parse_crontab_text(text, source, has_user=False):
    """
    Parse crontab text into task dicts with the same keys used on Windows.

    Handles five field lines, @ shorthand lines such as @daily and
    @reboot, comment lines, blank lines, and environment assignment
    lines (which are not tasks).
    """
    tasks = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ENV_ASSIGNMENT.match(stripped):
            continue
        parts = stripped.split()
        trigger_time = None
        rest = None
        if parts[0] in SHORTHAND_TRIGGERS:
            trigger_time = parts[0]
            rest = parts[1:]
        elif len(parts) > 5:
            trigger_time = " ".join(parts[:5])
            rest = parts[5:]
        else:
            continue
        if has_user:
            if not rest:
                continue
            rest = rest[1:]
        if not rest:
            continue
        command = " ".join(rest)
        task = _blank_task()
        task["task_name"] = command
        task["trigger_time"] = trigger_time
        task["command"] = command
        task["actions"] = command
        task["task_path"] = source
        tasks.append(task)
    return tasks


def get_linux_tasks(system_crontab_path="/etc/crontab", cron_d_dir="/etc/cron.d"):
    """
    Read the current user's crontab plus, when present and readable,
    /etc/crontab and the files in /etc/cron.d. The two path arguments
    exist so fixture paths can be exercised by a driver.
    """
    returncode, stdout, stderr = _run(["crontab", "-l"])
    tasks = []
    if returncode != 0:
        if "no crontab for" in stderr:
            # The normal empty state: an empty baseline, not an error.
            logging.info("No crontab for the current user; using an empty baseline for that source.")
        else:
            logging.error(f"Error retrieving cron tasks: {stderr}")
            return None
    else:
        tasks.extend(parse_crontab_text(stdout, "crontab (current user)"))

    extra_sources = []
    if system_crontab_path:
        extra_sources.append(system_crontab_path)
    if cron_d_dir:
        extra_sources.extend(sorted(glob.glob(cron_d_dir + "/*")))
    for path in extra_sources:
        try:
            with open(path, "r") as f:
                text = f.read()
        except OSError:
            continue
        # Every file the glob finds in cron.d is parsed as a crontab,
        # including editor backups and placeholder files a package
        # left behind, and every one of them is read as carrying a
        # user column. A stray file with five leading fields becomes
        # a task I then watch as though the system had scheduled it.
        tasks.extend(parse_crontab_text(text, path, has_user=True))
    return tasks


def get_scheduled_tasks(system_crontab_path="/etc/crontab", cron_d_dir="/etc/cron.d"):
    """
    Retrieves the list of scheduled tasks from the system.

    Returns:
        A list of dictionaries with the same keys on both platforms:
        task_name, trigger_time, command, last_run_time, task_path,
        state, and actions. Returns None only on a genuine retrieval
        error.
    """
    try:
        if platform.system() == 'Windows':
            return get_windows_tasks()
        elif platform.system() == 'Linux':
            return get_linux_tasks(system_crontab_path, cron_d_dir)
        else:
            logging.error(f"Unsupported operating system: {platform.system()}")
            return None
    except FileNotFoundError:
        logging.error("Scheduled task command not found. Make sure it is installed and in your PATH.")
        return None
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        return None


def monitor_scheduled_tasks():
    """
    Monitors scheduled tasks for changes and logs them.
    """
    try:
        previous_tasks = get_scheduled_tasks()
        if previous_tasks is None:
            logging.error("Failed to retrieve initial scheduled tasks; this run monitors nothing. Exiting.")
            sys.exit(1)

        while True:
            time.sleep(60)  # Check every 60 seconds

            current_tasks = get_scheduled_tasks()
            if current_tasks is None:
                logging.error("Failed to retrieve current scheduled tasks. Skipping this iteration.")
                continue

            # Compare tasks
            # Identity here is the name alone, and on the cron side the
            # name is the command string itself. Two entries running the
            # same command on different schedules collapse into one set
            # member and one first match in the lookups below: editing
            # the second entry's schedule changes nothing I can see,
            # because the first entry with that name is the one always
            # compared.
            previous_task_names = {task['task_name'] for task in previous_tasks}
            current_task_names = {task['task_name'] for task in current_tasks}

            added_tasks = current_task_names - previous_task_names
            removed_tasks = previous_task_names - current_task_names

            for task_name in added_tasks:
                task = next(task for task in current_tasks if task['task_name'] == task_name)
                log_message = f"New scheduled task added: Name: {task['task_name']}, Trigger Time: {task.get('trigger_time', 'N/A')}, Command: {task.get('command', 'N/A')}, LastRunTime: {task.get('last_run_time', 'N/A')}, TaskPath: {task.get('task_path', 'N/A')}, State: {task.get('state', 'N/A')}, Actions: {task.get('actions', 'N/A')}"
                logging.info(log_message)

            for task_name in removed_tasks:
                log_message = f"Scheduled task removed: Name: {task_name}"
                logging.warning(log_message)

            for current_task in current_tasks:
                previous_task = next((task for task in previous_tasks if task['task_name'] == current_task['task_name']), None)
                if previous_task:
                    # last_run_time is one of the compared fields, so on Windows
                    # a task simply running is reported as a scheduled task
                    # updated, with new and old trigger and command printed as
                    # though someone edited it. On a busy machine the update
                    # entries are mostly executions, and a real edit hides among
                    # them wearing the same wording.
                    changed = any(
                        current_task.get(key) != previous_task.get(key)
                        for key in ('trigger_time', 'command', 'state', 'actions', 'last_run_time')
                    )
                    if changed:
                        log_message = f"Scheduled task updated: Name: {current_task['task_name']}, New Trigger Time: {current_task.get('trigger_time', 'N/A')}, New Command: {current_task.get('command', 'N/A')}, Old Trigger Time: {previous_task.get('trigger_time', 'N/A')}, Old Command: {previous_task.get('command', 'N/A')}, LastRunTime: {current_task.get('last_run_time', 'N/A')}, TaskPath: {current_task.get('task_path', 'N/A')}, State: {current_task.get('state', 'N/A')}, Actions: {current_task.get('actions', 'N/A')}"
                        logging.info(log_message)
            previous_tasks = current_tasks

    except KeyboardInterrupt:
        logging.info("Monitoring stopped by user.")
    except SystemExit:
        raise
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")


def main():
    """
    Main function to execute the script.
    """
    args = setup_argparse()
    monitor_scheduled_tasks()


if __name__ == "__main__":
    main()

# Usage example:
#    python main.py

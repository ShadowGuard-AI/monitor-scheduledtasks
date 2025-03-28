import argparse
import logging
import subprocess
import shlex
import time
import os
import platform

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def setup_argparse():
    """
    Sets up the argument parser for the script.
    """
    parser = argparse.ArgumentParser(description="Monitors changes to scheduled tasks and logs them.")
    # No specific CLI arguments are defined in the prompt, so we leave it empty for now.
    # Consider adding arguments such as log file path, polling interval, etc.

    # Example additions (remove the comment to enable):
    # parser.add_argument("-l", "--log-file", dest="log_file", help="Path to the log file.", default="scheduled_tasks.log")
    # parser.add_argument("-i", "--interval", dest="interval", type=int, help="Polling interval in seconds.", default=60)
    return parser.parse_args()

def get_scheduled_tasks():
    """
    Retrieves the list of scheduled tasks from the system.

    Returns:
        A list of dictionaries, where each dictionary represents a scheduled task
        and contains task name, trigger time, and command.
        Returns None if an error occurs.
    """
    try:
        # Platform specific command to get scheduled tasks
        if platform.system() == 'Windows':
            command = "powershell Get-ScheduledTask | Select-Object TaskName, LastRunTime, TaskPath, State, Actions"
            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate()
            if stderr:
                logging.error(f"Error retrieving scheduled tasks: {stderr}")
                return None

            tasks = []
            for line in stdout.splitlines():
                if line.startswith("TaskName"):
                    continue # Skip header line
                if line.strip(): #Handle empty lines
                    parts = line.split(maxsplit=1)
                    if len(parts) >= 1:
                        task_name = parts[0].strip()

                        command_taskpath = "powershell Get-ScheduledTask -TaskName \"{}\" | Select-Object TaskName, LastRunTime, TaskPath, State, Actions".format(task_name)
                        process_taskpath = subprocess.Popen(command_taskpath, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        stdout_taskpath, stderr_taskpath = process_taskpath.communicate()

                        if stderr_taskpath:
                            logging.error(f"Error retrieving task path for {task_name}: {stderr_taskpath}")
                            continue

                        last_run_time = None
                        task_path = None
                        state = None
                        actions = None

                        for taskline in stdout_taskpath.splitlines():
                             if "LastRunTime" in taskline:
                                 last_run_time = taskline.split(":",1)[1].strip()
                             if "TaskPath" in taskline:
                                 task_path = taskline.split(":",1)[1].strip()
                             if "State" in taskline:
                                  state = taskline.split(":", 1)[1].strip()
                             if "Actions" in taskline:
                                  actions = taskline.split(":", 1)[1].strip()

                        tasks.append({
                            "task_name": task_name,
                            "last_run_time": last_run_time,
                            "task_path": task_path,
                            "state": state,
                            "actions": actions
                        })
            return tasks

        elif platform.system() == 'Linux': # Example for Linux using cron (might need adaptation based on the actual scheduler)
            command = "crontab -l"
            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate()
            if stderr:
                logging.error(f"Error retrieving cron tasks: {stderr}")
                return None

            tasks = []
            for line in stdout.splitlines():
                if line.strip() and not line.startswith("#"): # Ignore comments and empty lines
                    parts = line.split()
                    if len(parts) > 5:
                        trigger_time = " ".join(parts[:5])
                        command = " ".join(parts[5:])
                        tasks.append({
                            "task_name": command,  # Using command as task name for simplicity
                            "trigger_time": trigger_time,
                            "command": command
                        })
            return tasks
        else:
            logging.error(f"Unsupported operating system: {platform.system()}")
            return None

    except FileNotFoundError:
        logging.error("crontab command not found. Make sure it is installed and in your PATH.")
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
            logging.error("Failed to retrieve initial scheduled tasks. Exiting.")
            return

        while True:
            time.sleep(60)  # Check every 60 seconds (configurable)

            current_tasks = get_scheduled_tasks()
            if current_tasks is None:
                logging.error("Failed to retrieve current scheduled tasks. Skipping this iteration.")
                continue

            # Compare tasks
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
                  if current_task.get('trigger_time') != previous_task.get('trigger_time') or current_task.get('command') != previous_task.get('command'):

                     log_message = f"Scheduled task updated: Name: {current_task['task_name']}, New Trigger Time: {current_task.get('trigger_time', 'N/A')}, New Command: {current_task.get('command', 'N/A')}, Old Trigger Time: {previous_task.get('trigger_time', 'N/A')}, Old Command: {previous_task.get('command', 'N/A')}, LastRunTime: {current_task.get('last_run_time', 'N/A')}, TaskPath: {current_task.get('task_path', 'N/A')}, State: {current_task.get('state', 'N/A')}, Actions: {current_task.get('actions', 'N/A')}"
                     logging.info(log_message)
            previous_tasks = current_tasks

    except KeyboardInterrupt:
        logging.info("Monitoring stopped by user.")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")

def main():
    """
    Main function to execute the script.
    """
    args = setup_argparse()

    # Example usage of CLI args: (remove comment to enable)
    # log_file = args.log_file
    # interval = args.interval
    # logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    # logging.info("Scheduled task monitoring started.")
    monitor_scheduled_tasks()

if __name__ == "__main__":
    main()

# Usage examples:
# 1. Run the script with default settings:
#    python main.py

# 2. (If CLI args are added): Run the script with a custom log file and interval:
#    python main.py -l my_tasks.log -i 120
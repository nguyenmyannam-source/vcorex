# core/pid_lock.py
"""
A simple PID-based lock mechanism to prevent multiple instances of the bot from running.
This is a critical safety feature for production environments.
"""

import os
import psutil
from loguru import logger

LOCK_FILE_NAME = "vcorex.pid"

class PIDLock:
    """
    Manages a lock file containing the Process ID (PID) of the running bot instance.
    """

    def __init__(self, lock_file_path: str = LOCK_FILE_NAME):
        """
        Initializes the lock with a specific file path.

        Args:
            lock_file_path (str): The path to the lock file. Defaults to 'vcorex.pid'.
        """
        self.lock_file_path = lock_file_path
        self._is_locked = False

    def acquire(self) -> bool:
        """
        Attempts to acquire the lock.

        Returns:
            bool: True if the lock was acquired successfully, False otherwise.
        """
        if os.path.exists(self.lock_file_path):
            logger.warning(f"Lock file '{self.lock_file_path}' already exists. Checking for stale lock.")
            try:
                with open(self.lock_file_path, "r") as f:
                    stale_pid = int(f.read().strip())
                
                if psutil.pid_exists(stale_pid):
                    # The process is still running. This is a duplicate instance.
                    try:
                        p = psutil.Process(stale_pid)
                        logger.critical(
                            f"Another instance of the bot is already running with PID {stale_pid} "
                            f"(name: {p.name()}, created: {p.create_time()}). Aborting startup."
                        )
                    except psutil.NoSuchProcess:
                         # Race condition: process died between pid_exists and Process()
                         logger.warning("Stale PID process disappeared. Treating as a stale lock.")
                         self._create_lock_file()
                         return True
                    return False
                else:
                    # The process is dead. This is a stale lock.
                    logger.warning(f"Stale lock file found for dead PID {stale_pid}. Cleaning up and taking over.")
                    self._create_lock_file()
                    return True

            except (IOError, ValueError) as e:
                logger.error(f"Unable to read or parse lock file '{self.lock_file_path}'. Error: {e}. Please remove it manually.")
                return False
        else:
            # Lock file does not exist, we can acquire the lock.
            self._create_lock_file()
            return True

    def release(self):
        """
        Releases the lock by deleting the lock file.
        """
        if self._is_locked:
            try:
                if os.path.exists(self.lock_file_path):
                    # Verify it's our lock before deleting
                    with open(self.lock_file_path, "r") as f:
                        pid_in_file = int(f.read().strip())
                    if pid_in_file == os.getpid():
                        os.remove(self.lock_file_path)
                        logger.info(f"Lock file '{self.lock_file_path}' released successfully.")
                    else:
                        logger.warning(f"Not releasing lock file as it is owned by another PID ({pid_in_file}).")
                self._is_locked = False
            except (IOError, ValueError) as e:
                logger.error(f"Error releasing lock file: {e}")

    def _create_lock_file(self):
        """
        Creates the lock file and writes the current PID to it.
        """
        try:
            current_pid = os.getpid()
            with open(self.lock_file_path, "w") as f:
                f.write(str(current_pid))
            self._is_locked = True
            logger.info(f"Lock acquired. Bot running with PID {current_pid}. Lock file: '{self.lock_file_path}'")
        except IOError as e:
            logger.error(f"Failed to create lock file '{self.lock_file_path}': {e}")
            self._is_locked = False
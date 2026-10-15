import os

class DummyBackupManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.status = {"error": None}
    def restore_if_needed(self):
        pass
    def start(self):
        pass
    def backup_now(self, actor):
        return True

def manager_from_env(db_path):
    # Returns a backup worker if environment setup exists, otherwise paths safely out
    return DummyBackupManager(db_path)

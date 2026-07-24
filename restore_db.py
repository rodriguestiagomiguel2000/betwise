# restore_db.py
import os
import shutil
from datetime import datetime

def restore_latest_backup():
    """Restaura o backup mais recente"""
    backup_dir = 'backups'
    
    if not os.path.exists(backup_dir):
        print("❌ Nenhum backup encontrado")
        return
    
    backups = sorted([f for f in os.listdir(backup_dir) if f.startswith('bets_db_')])
    
    if not backups:
        print("❌ Nenhum backup encontrado")
        return
    
    latest = backups[-1]
    backup_path = os.path.join(backup_dir, latest)
    db_path = 'instance/bets.db'
    
    # Fazer backup do atual
    if os.path.exists(db_path):
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        shutil.copy2(db_path, f'instance/bets_db_before_restore_{timestamp}.db')
    
    # Restaurar
    shutil.copy2(backup_path, db_path)
    print(f"✅ Backup restaurado: {latest}")

if __name__ == "__main__":
    restore_latest_backup()
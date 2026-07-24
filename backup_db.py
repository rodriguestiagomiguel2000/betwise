# backup_db.py
import os
import shutil
import json
from datetime import datetime

def backup_database():
    """Faz backup da base de dados SQLite para um ficheiro JSON"""
    db_path = 'instance/bets.db'
    
    if not os.path.exists(db_path):
        print("❌ Base de dados não encontrada")
        return
    
    # Criar pasta de backups
    os.makedirs('backups', exist_ok=True)
    
    # Copiar o ficheiro
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    backup_path = f'backups/bets_db_{timestamp}.db'
    shutil.copy2(db_path, backup_path)
    
    # Manter apenas os últimos 10 backups
    backups = sorted([f for f in os.listdir('backups') if f.startswith('bets_db_')])
    if len(backups) > 10:
        for old in backups[:-10]:
            os.remove(f'backups/{old}')
    
    print(f"✅ Backup criado: {backup_path}")

if __name__ == "__main__":
    backup_database()
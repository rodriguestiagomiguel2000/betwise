# import_user_render.py
from app import app, db, User
import json
import os

with app.app_context():
    # Verificar se o ficheiro existe
    if not os.path.exists('my_user.json'):
        print("❌ Ficheiro my_user.json não encontrado!")
        exit()
    
    # Ler o ficheiro
    with open('my_user.json', 'r') as f:
        data = json.load(f)
    
    print(f"📥 Importando utilizador: {data['username']}")
    
    # Verificar se o utilizador já existe
    user = User.query.filter_by(username=data['username']).first()
    if user:
        print(f"⚠️ Utilizador {data['username']} já existe. A atualizar...")
        user.email = data['email']
        user.password_hash = data['password_hash']
        user.is_admin = data['is_admin']
        user.is_active = data['is_active']
        db.session.commit()
        print(f"✅ Utilizador {data['username']} atualizado!")
    else:
        # Criar utilizador
        user = User(
            username=data['username'],
            email=data['email'],
            password_hash=data['password_hash'],
            is_admin=data['is_admin'],
            is_active=data['is_active']
        )
        db.session.add(user)
        db.session.commit()
        print(f"✅ Utilizador {data['username']} importado com sucesso!")
    
    # Listar utilizadores
    print("\n📋 Utilizadores existentes:")
    for u in User.query.all():
        print(f"   - {u.username}")
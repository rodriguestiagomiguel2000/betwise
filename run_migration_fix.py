# fix_all.py
from app import app, db, User, Bankroll, Bookmaker, Bet
from werkzeug.security import generate_password_hash

with app.app_context():
    print("=" * 50)
    print("🔧 FIXING DATABASE")
    print("=" * 50)
    
    # 1. Criar admin
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(
            username='admin',
            email='admin@metrikatips.com',
            password_hash=generate_password_hash('admin123'),
            is_admin=True,
            is_active=True
        )
        db.session.add(admin)
        db.session.commit()
        print("✅ Admin user created!")
    else:
        print("✅ Admin user already exists.")
    
    admin_id = admin.id
    
    # 2. Atualizar registos existentes
    count = Bankroll.query.filter(Bankroll.user_id.is_(None)).update({Bankroll.user_id: admin_id})
    print(f"✅ Updated {count} bankrolls")
    
    count = Bookmaker.query.filter(Bookmaker.user_id.is_(None)).update({Bookmaker.user_id: admin_id})
    print(f"✅ Updated {count} bookmakers")
    
    count = Bet.query.filter(Bet.user_id.is_(None)).update({Bet.user_id: admin_id})
    print(f"✅ Updated {count} bets")
    
    db.session.commit()
    print("=" * 50)
    print("✅ ALL DONE!")
    print("=" * 50)
    print(f"👤 Login with: admin / admin123")
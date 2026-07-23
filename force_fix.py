# force_fix.py
from app import app, db, User, Bankroll, Bookmaker, Bet, Transaction, BankrollBookmakerBalance

with app.app_context():
    print("=" * 50)
    print("🔧 FORÇAR CORREÇÃO")
    print("=" * 50)
    
    # Garantir que o admin existe
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        from werkzeug.security import generate_password_hash
        admin = User(
            username='admin',
            email='admin@metrikatips.com',
            password_hash=generate_password_hash('admin123'),
            is_admin=True,
            is_active=True
        )
        db.session.add(admin)
        db.session.commit()
        print("✅ Admin criado!")
    else:
        print(f"✅ Admin encontrado (ID: {admin.id})")
    
    admin_id = admin.id
    
    # Forçar atualização de TODOS os registos
    print(f"\n📌 A atualizar todos os registos para user_id = {admin_id}")
    
    # Bankrolls
    count = Bankroll.query.update({Bankroll.user_id: admin_id})
    print(f"   ✅ Bankrolls: {count} atualizados")
    
    # Bookmakers
    count = Bookmaker.query.update({Bookmaker.user_id: admin_id})
    print(f"   ✅ Bookmakers: {count} atualizados")
    
    # Bets
    count = Bet.query.update({Bet.user_id: admin_id})
    print(f"   ✅ Bets: {count} atualizados")
    
    # Transactions (não têm user_id, mas vamos verificar)
    print(f"   ℹ️ Transactions: {Transaction.query.count()} (não têm user_id)")
    
    # BankrollBookmakerBalance (não têm user_id)
    print(f"   ℹ️ BankrollBookmakerBalance: {BankrollBookmakerBalance.query.count()} (não têm user_id)")
    
    db.session.commit()
    print("\n✅ TODOS OS REGISTOS ATUALIZADOS!")
    print("=" * 50)
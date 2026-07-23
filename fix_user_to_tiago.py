# fix_user_to_tiago.py
from app import app, db, User, Bankroll, Bookmaker, Bet

with app.app_context():
    # Encontrar o utilizador tiago32rodriguez
    tiago = User.query.filter_by(username='tiago32rodriguez').first()
    admin = User.query.filter_by(username='admin').first()
    
    if not tiago:
        print("❌ Utilizador tiago32rodriguez não encontrado!")
        exit()
    
    print(f"👤 tiago32rodriguez ID: {tiago.id}")
    print(f"👤 admin ID: {admin.id if admin else 'N/A'}")
    
    # Atualizar todos os registos do admin para o tiago
    print("\n📌 A mover dados do admin para tiago32rodriguez...")
    
    # Bankrolls
    count = Bankroll.query.filter_by(user_id=admin.id).update({Bankroll.user_id: tiago.id})
    print(f"   ✅ Bankrolls: {count} movidos")
    
    # Bookmakers
    count = Bookmaker.query.filter_by(user_id=admin.id).update({Bookmaker.user_id: tiago.id})
    print(f"   ✅ Bookmakers: {count} movidos")
    
    # Bets
    count = Bet.query.filter_by(user_id=admin.id).update({Bet.user_id: tiago.id})
    print(f"   ✅ Bets: {count} movidos")
    
    db.session.commit()
    
    print("\n✅ TODOS OS DADOS MOVIDOS PARA tiago32rodriguez!")
    print("=" * 50)
    print("🔑 Faz login com:")
    print("   Username: tiago32rodriguez")
    print("   Password: [a tua password]")
# direct_check.py
import sqlite3

conn = sqlite3.connect('bets.db')
cursor = conn.cursor()

print("=" * 50)
print("📊 CHECK SQLITE DIRETO")
print("=" * 50)

# Verificar bankrolls
cursor.execute("SELECT id, name, user_id FROM bankroll")
rows = cursor.fetchall()
print(f"🏦 Bankrolls: {len(rows)}")
for row in rows:
    print(f"   - ID: {row[0]}, Name: {row[1]}, user_id: {row[2]}")

print()

# Verificar bookmakers
cursor.execute("SELECT id, name, user_id FROM bookmaker")
rows = cursor.fetchall()
print(f"📚 Bookmakers: {len(rows)}")
for row in rows:
    print(f"   - ID: {row[0]}, Name: {row[1]}, user_id: {row[2]}")

print()

# Verificar bets
cursor.execute("SELECT id, user_id FROM bet")
rows = cursor.fetchall()
print(f"🎲 Bets: {len(rows)}")
for row in rows:
    print(f"   - ID: {row[0]}, user_id: {row[1]}")

conn.close()
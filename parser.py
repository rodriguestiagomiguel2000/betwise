from datetime import datetime
from typing import Any, Dict, List, Optional

def parse_betslip_from_gemini(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Take the raw JSON from Gemini and normalize it for app.py.
    """
    from datetime import datetime
    
    out: Dict[str, Any] = {}

    # Top-level fields
    out["bookmaker"] = data.get("bookmaker")
    out["sport"] = data.get("sport")
    out["market_type"] = data.get("market_type")
    out["stake"] = data.get("stake")
    out["potential_return"] = data.get("potential_return")
    out["currency"] = data.get("currency")
    out["status"] = data.get("status")
    out["bet_id"] = data.get("bet_id")

    # placed_at: ISO string -> datetime
    placed_raw = data.get("placed_at")
    current_year = datetime.utcnow().year
    
    if placed_raw:
        try:
            # Tentar fazer parse da data
            dt = None
            
            # Tentar diferentes formatos
            # Formato completo: YYYY-MM-DDTHH:MM:SS
            try:
                dt = datetime.fromisoformat(placed_raw)
            except ValueError:
                pass
            
            # Se falhou, tentar apenas data: YYYY-MM-DD
            if not dt:
                try:
                    dt = datetime.strptime(placed_raw.split('T')[0], '%Y-%m-%d')
                except ValueError:
                    pass
            
            # Se ainda falhou, tentar DD/MM/YYYY ou DD/MM/YY
            if not dt:
                for fmt in ['%d/%m/%Y', '%d/%m/%y', '%d-%m-%Y', '%d-%m-%y']:
                    try:
                        dt = datetime.strptime(placed_raw, fmt)
                        break
                    except ValueError:
                        continue
            
            # Se falhou tudo, tentar extrair apenas dia e mês
            if not dt:
                import re
                # Procurar padrão DD/MM ou DD-MM
                match = re.search(r'(\d{1,2})[/-](\d{1,2})', placed_raw)
                if match:
                    day = int(match.group(1))
                    month = int(match.group(2))
                    # Usar ano atual
                    try:
                        dt = datetime(current_year, month, day)
                    except ValueError:
                        dt = None
            
            if dt:
                # Verificar se a data é razoável (entre 2020 e 2030)
                if dt.year < 2020 or dt.year > 2030:
                    # Se for uma data muito antiga ou futura, usar ano atual
                    try:
                        dt = datetime(current_year, dt.month, dt.day)
                    except ValueError:
                        dt = None
            
            out["placed_at"] = dt
            
        except Exception as e:
            print(f"DEBUG: Error parsing date '{placed_raw}': {e}")
            out["placed_at"] = None
    else:
        out["placed_at"] = None

    # Legs
    legs_in: List[Dict[str, Any]] = data.get("legs") or []
    legs_out: List[Dict[str, Any]] = []

    for leg in legs_in:
        legs_out.append(
            {
                "event": leg.get("event"),
                "team": leg.get("team"),
                "market": leg.get("market"),
                "odds_decimal": leg.get("odds_decimal"),
            }
        )

    out["legs"] = legs_out

    # total_odds
    total_odds: Optional[float] = None
    if isinstance(data.get("total_odds"), (int, float)):
        total_odds = float(data["total_odds"])
    else:
        product = 1.0
        any_odds = False
        for leg in legs_out:
            odds = leg.get("odds_decimal")
            if isinstance(odds, (int, float)):
                product *= float(odds)
                any_odds = True
        if any_odds:
            total_odds = round(product, 3)

    out["total_odds"] = total_odds

    return out
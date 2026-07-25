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

    # ===== EXTRAIR DATA/HORA DO PRIMEIRO EVENTO =====
    legs_in: List[Dict[str, Any]] = data.get("legs") or []
    legs_out: List[Dict[str, Any]] = []
    
    # Data/hora extraída do primeiro evento
    extracted_datetime = None
    
    for leg in legs_in:
        leg_data = {
            "event": leg.get("event"),
            "team": leg.get("team"),
            "market": leg.get("market"),
            "odds_decimal": leg.get("odds_decimal"),
        }
        legs_out.append(leg_data)
        
        # Tentar extrair data/hora do evento (se ainda não foi extraída)
        if extracted_datetime is None and leg.get("event"):
            extracted_datetime = extract_datetime_from_event(leg.get("event"))
    
    out["legs"] = legs_out

    # ===== PROCESSAR placed_at =====
    placed_raw = data.get("placed_at")
    current_year = datetime.utcnow().year
    
    # Tentar usar a data/hora extraída do evento
    if extracted_datetime:
        out["placed_at"] = extracted_datetime
    elif placed_raw:
        # Tentar fazer parse da data do Gemini
        try:
            dt = None
            for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y', '%d/%m/%y']:
                try:
                    dt = datetime.strptime(placed_raw, fmt)
                    break
                except ValueError:
                    continue
            
            if dt:
                # Verificar se a data é razoável
                if dt.year < 2020 or dt.year > 2030:
                    dt = datetime(current_year, dt.month, dt.day) if dt.month else None
                out["placed_at"] = dt
            else:
                out["placed_at"] = None
        except Exception:
            out["placed_at"] = None
    else:
        out["placed_at"] = None

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


def extract_datetime_from_event(event_text: str) -> Optional[datetime]:
    """Extrai data e hora do texto de um evento."""
    if not event_text:
        return None
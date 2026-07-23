# value_detector.py
import math
import os

class ValueBetDetector:
    def __init__(self, bets, use_api=True):
        """
        Inicializa o detector com uma lista de bets já carregadas
        
        Args:
            bets: Lista de objetos Bet (já carregados do banco)
            use_api: Se deve tentar usar a API de odds
        """
        self.bets = bets
        self.use_api = use_api
        
        if use_api:
            try:
                from odds_api import OddsAPI
                self.odds_api = OddsAPI()
            except (ValueError, ImportError) as e:
                print(f"⚠️ Odds API not available: {e}")
                self.use_api = False
    
    def get_historical_win_rate(self, sport=None, odds_range=None, market_type=None):
        """Calcula win rate histórico para filtros específicos"""
        filtered = self.bets
        
        if sport:
            filtered = [b for b in filtered if b.sport == sport]
        if market_type:
            filtered = [b for b in filtered if b.market_type == market_type]
        if odds_range:
            min_odds, max_odds = odds_range
            filtered = [b for b in filtered if min_odds <= (b.total_odds or 0) <= max_odds]
        
        # Apenas apostas resolvidas
        resolved = [b for b in filtered if b.status in ['won', 'lost']]
        if not resolved:
            return 0.5
        
        won = sum(1 for b in resolved if b.status == "won")
        return won / len(resolved)
    
    def get_avg_odds(self, sport=None, market_type=None):
        """Calcula a odd média para filtros específicos"""
        filtered = self.bets
        
        if sport:
            filtered = [b for b in filtered if b.sport == sport]
        if market_type:
            filtered = [b for b in filtered if b.market_type == market_type]
        
        odds = [b.total_odds for b in filtered if b.total_odds and b.total_odds > 0]
        if not odds:
            return 0
        
        return sum(odds) / len(odds)
    
    def get_market_probability(self, sport, total_odds):
        """
        Obtém probabilidade real do mercado via API
        """
        if not self.use_api:
            return None
        
        try:
            # Mapear desporto para formato da API
            sport_map = {
                'Football': 'soccer_epl',
                'Basketball': 'basketball_nba',
                'Tennis': 'tennis_atp',
                'Ice Hockey': 'icehockey_nhl',
                'Baseball': 'baseball_mlb'
            }
            api_sport = sport_map.get(sport, 'upcoming')
            
            odds_data = self.odds_api.get_odds(sport=api_sport, markets='h2h')
            
            if odds_data and len(odds_data) > 0:
                all_odds = []
                for event in odds_data[:10]:
                    for bookmaker in event.get('bookmakers', []):
                        for market in bookmaker.get('markets', []):
                            for outcome in market.get('outcomes', []):
                                price = outcome.get('price')
                                if price and price > 1:
                                    all_odds.append(price)
                
                if all_odds:
                    # Média das odds do mercado
                    avg_odds = sum(all_odds) / len(all_odds)
                    return 1 / avg_odds
        except Exception as e:
            print(f"⚠️ Error fetching odds from API: {e}")
        
        return None
    
    def detect_value(self, bet):
        """Detecta se uma aposta tem valor"""
        if not bet.total_odds or bet.total_odds <= 1:
            return None
        
        # Probabilidade implícita das odds
        implied_prob = 1 / bet.total_odds
        
        # Tentar obter probabilidade do mercado via API
        market_prob = self.get_market_probability(bet.sport, bet.total_odds)
        
        if market_prob:
            real_prob = market_prob
            source = 'API'
        else:
            # Fallback: usar win rate histórico
            real_prob = self.get_historical_win_rate(
                sport=bet.sport,
                market_type=bet.market_type
            )
            source = 'Historical'
        
        # Se a probabilidade real for maior que a implícita, há value
        if real_prob > implied_prob:
            value_pct = (real_prob - implied_prob) * 100
            expected_value = (bet.total_odds * real_prob) - 1
            
            return {
                'value_pct': round(value_pct, 1),
                'real_prob': round(real_prob * 100, 1),
                'implied_prob': round(implied_prob * 100, 1),
                'expected_value': round(expected_value * 100, 1),
                'source': source,
                'recommendation': self._get_recommendation(value_pct, expected_value),
                'confidence': self._get_confidence(real_prob)
            }
        return None
    
    def _get_recommendation(self, value_pct, expected_value):
        """Recomendação baseada no valor e expected value"""
        if value_pct > 10 and expected_value > 0.15:
            return '🔥 Strong Value Bet!'
        elif value_pct > 5 and expected_value > 0.05:
            return '✅ Value Bet Detected'
        elif value_pct > 2:
            return '⚠️ Slight Value'
        else:
            return 'ℹ️ Marginal Value'
    
    def _get_confidence(self, real_prob):
        """Nível de confiança baseado na probabilidade real"""
        if real_prob > 0.65:
            return 'High'
        elif real_prob > 0.55:
            return 'Medium'
        else:
            return 'Low'
    
    def get_best_value_bets(self, limit=10):
        """Retorna as melhores apostas com valor"""
        open_bets = [b for b in self.bets if b.status == 'open']
        
        value_bets = []
        for bet in open_bets:
            result = self.detect_value(bet)
            if result:
                value_bets.append({
                    'bet': bet,
                    'value': result
                })
        
        # Ordenar por value_pct (maior primeiro)
        value_bets.sort(key=lambda x: x['value']['value_pct'], reverse=True)
        
        return value_bets[:limit]
    
    def get_statistics(self):
        """Retorna estatísticas gerais do utilizador"""
        total = len(self.bets)
        won = sum(1 for b in self.bets if b.status == 'won')
        lost = sum(1 for b in self.bets if b.status == 'lost')
        resolved = won + lost
        
        return {
            'total_bets': total,
            'won': won,
            'lost': lost,
            'win_rate': round(won / resolved * 100, 1) if resolved > 0 else 0,
            'avg_odds': self.get_avg_odds(),
            'sports': list(set(b.sport for b in self.bets if b.sport))
        }
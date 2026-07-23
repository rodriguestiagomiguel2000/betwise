# odds_api.py
import requests
import os
from datetime import datetime

class OddsAPI:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get('ODDS_API_KEY')
        self.base_url = "https://api.the-odds-api.com/v4"
        
        if not self.api_key:
            raise ValueError("API key is required. Get one at https://the-odds-api.com/")
    
    def get_sports(self):
        """Lista todos os desportos disponíveis"""
        url = f"{self.base_url}/sports"
        params = {'apiKey': self.api_key}
        response = requests.get(url, params=params)
        return response.json()
    
    def get_odds(self, sport='upcoming', regions='eu,us,uk', markets='h2h'):
        """
        Obtém odds para um desporto específico
        
        Args:
            sport: 'upcoming' para todos, ou 'soccer_epl', 'basketball_nba', etc.
            regions: 'eu,us,uk,au'
            markets: 'h2h', 'spreads', 'totals', 'outrights'
        """
        url = f"{self.base_url}/sports/{sport}/odds"
        params = {
            'apiKey': self.api_key,
            'regions': regions,
            'markets': markets,
            'oddsFormat': 'decimal'
        }
        response = requests.get(url, params=params)
        
        # Verificar quota restante
        remaining = response.headers.get('x-requests-remaining')
        if remaining:
            print(f"🔑 Requests remaining: {remaining}")
        
        return response.json()
    
    def get_event_odds(self, sport, event_id, regions='eu,us,uk'):
        """Obtém odds para um evento específico"""
        url = f"{self.base_url}/sports/{sport}/events/{event_id}/odds"
        params = {
            'apiKey': self.api_key,
            'regions': regions,
            'oddsFormat': 'decimal'
        }
        response = requests.get(url, params=params)
        return response.json()

    def get_probability_from_odds(self, decimal_odds):
        """Converte odds decimais em probabilidade implícita"""
        if decimal_odds and decimal_odds > 1:
            return 1 / decimal_odds
        return None

    def calculate_value(self, decimal_odds, estimated_probability):
        """Calcula o valor de uma aposta"""
        implied_prob = self.get_probability_from_odds(decimal_odds)
        if implied_prob is None:
            return None
        
        value = estimated_probability - implied_prob
        expected_value = (estimated_probability * decimal_odds) - 1
        
        return {
            'implied_probability': round(implied_prob * 100, 1),
            'estimated_probability': round(estimated_probability * 100, 1),
            'value': round(value * 100, 1),
            'expected_value': round(expected_value * 100, 1),
            'is_value': value > 0
        }
import json
import re
from typing import Dict, Any, Tuple, List
from openai import OpenAI, APIConnectionError, APIError


class AITrader:
    def __init__(self, api_key: str, api_url: str, model_name: str,
                 system_prompt: str = '', user_prompt: str = '', enable_cot: bool = False):
        self.api_key = api_key
        self.api_url = api_url
        self.model_name = model_name
        self.system_prompt = (system_prompt or '').strip()
        self.user_prompt = (user_prompt or '').strip()
        self.enable_cot = bool(enable_cot)
    
    def make_decision(self, market_state: Dict, portfolio: Dict,
                      account_info: Dict) -> Dict[str, Any]:
        context_prompt = self._build_prompt(market_state, portfolio, account_info)
        user_message = self._compose_user_message(context_prompt)
        
        response = self._call_llm(user_message)
        
        decisions, cot_trace = self._parse_response(response)
        
        return {
            'decisions': decisions,
            'cot_trace': cot_trace
        }
    
    def _build_prompt(self, market_state: Dict, portfolio: Dict,
                      account_info: Dict) -> str:
        prompt = "You are a professional cryptocurrency trader. Analyze the market and make trading decisions.\n\n"
        prompt += "MARKET DATA:\n"
        for coin, data in market_state.items():
            prompt += f"{coin}: ${data['price']:.2f} ({data['change_24h']:+.2f}%)\n"
            if 'indicators' in data and data['indicators']:
                indicators = data['indicators']
                prompt += (
                    f"  SMA7: ${indicators.get('sma_7', 0):.2f}, "
                    f"SMA14: ${indicators.get('sma_14', 0):.2f}, "
                    f"RSI: {indicators.get('rsi_14', 0):.1f}\n"
                )
        
        prompt += (
            f"\nACCOUNT STATUS:\n"
            f"- Initial Capital: ${account_info['initial_capital']:.2f}\n"
            f"- Total Value: ${portfolio['total_value']:.2f}\n"
            f"- Cash: ${portfolio['cash']:.2f}\n"
            f"- Total Return: {account_info['total_return']:.2f}%\n\n"
            f"CURRENT POSITIONS:\n"
        )
        
        if portfolio['positions']:
            for pos in portfolio['positions']:
                prompt += (
                    f"- {pos['coin']} {pos['side']}: {pos['quantity']:.4f} "
                    f"@ ${pos['avg_price']:.2f} ({pos['leverage']}x)\n"
                )
        else:
            prompt += "None\n"
        
        prompt += """
TRADING RULES:
1. Signals: buy_to_enter (long), sell_to_enter (short), close_position, hold
2. Risk Management:
   - Max 3 positions
   - Risk 1-5% per trade
   - Use appropriate leverage (1-20x)
3. Position Sizing:
   - Conservative: 1-2% risk
   - Moderate: 2-4% risk
   - Aggressive: 4-5% risk
4. Exit Strategy:
   - Close losing positions quickly
   - Let winners run
   - Use technical indicators
"""
        
        if self.enable_cot:
            prompt += """
OUTPUT FORMAT (JSON only):
```json
{
  "decisions": {
    "COIN": {
      "signal": "buy_to_enter|sell_to_enter|hold|close_position",
      "quantity": 0.5,
      "leverage": 10,
      "profit_target": 45000.0,
      "stop_loss": 42000.0,
      "confidence": 0.75,
      "justification": "Brief reason"
    }
  },
  "cot_trace": [
    "Step-by-step reasoning explaining the decision making."
  ]
}
```

Analyze and output JSON only.
"""
        else:
            prompt += """
OUTPUT FORMAT (JSON only):
```json
{
  "COIN": {
    "signal": "buy_to_enter|sell_to_enter|hold|close_position",
    "quantity": 0.5,
    "leverage": 10,
    "profit_target": 45000.0,
    "stop_loss": 42000.0,
    "confidence": 0.75,
    "justification": "Brief reason"
  }
}
```

Analyze and output JSON only.
"""
        
        return prompt
    
    def _compose_user_message(self, context: str) -> str:
        if not self.user_prompt:
            return context
        
        if '{context}' in self.user_prompt:
            return self.user_prompt.replace('{context}', context)
        
        return f"{self.user_prompt}\n\n{context}"
    
    def _compose_system_prompt(self) -> str:
        base_prompt = self.system_prompt or "You are a professional cryptocurrency trader."
        base_prompt = base_prompt.strip()
        
        if self.enable_cot:
            output_instruction = (
                "Respond strictly in JSON with keys `decisions` and `cot_trace`. "
                "`cot_trace` must be an array of short reasoning strings."
            )
        else:
            output_instruction = "Respond strictly in JSON describing the trading decisions for each coin."
        
        format_instruction = "Do not include any additional text outside the JSON."
        
        return f"{base_prompt}\n\n{output_instruction}\n{format_instruction}"
    
    def _call_llm(self, user_message: str) -> str:
        try:
            base_url = self.api_url.rstrip('/')
            if not base_url.endswith('/v1'):
                if '/v1' in base_url:
                    base_url = base_url.split('/v1')[0] + '/v1'
                else:
                    base_url = base_url + '/v1'
            
            client = OpenAI(
                api_key=self.api_key,
                base_url=base_url
            )
            
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": self._compose_system_prompt()
                    },
                    {
                        "role": "user",
                        "content": user_message
                    }
                ],
                temperature=0.7,
                max_tokens=2000
            )
            
            return response.choices[0].message.content
        
        except APIConnectionError as e:
            error_msg = f"API connection failed: {str(e)}"
            print(f"[ERROR] {error_msg}")
            raise Exception(error_msg)
        except APIError as e:
            error_msg = f"API error ({e.status_code}): {e.message}"
            print(f"[ERROR] {error_msg}")
            raise Exception(error_msg)
        except Exception as e:
            error_msg = f"LLM call failed: {str(e)}"
            print(f"[ERROR] {error_msg}")
            import traceback
            print(traceback.format_exc())
            raise Exception(error_msg)
    
    def _strip_cot_segments(self, response: str) -> Tuple[str, List[str]]:
        """Remove known CoT tags (e.g. <think>...</think>) and collect their content."""
        if not response:
            return response, []

        cot_segments: List[str] = []

        def _collect(match: re.Match) -> str:
            content = match.group('content').strip()
            if content:
                cot_segments.append(content)
            return '\n'

        pattern = re.compile(
            r'<\s*(?P<tag>think|thinking|reasoning|cot)\b[^>]*>'
            r'(?P<content>.*?)'
            r'</\s*(?P=tag)\s*>',
            flags=re.IGNORECASE | re.DOTALL
        )
        cleaned = re.sub(pattern, _collect, response)
        return cleaned.strip(), cot_segments

    def _parse_response(self, response: str) -> Tuple[Dict, Any]:
        response = response.strip()
        response, cot_segments = self._strip_cot_segments(response)
        
        if '```json' in response:
            response = response.split('```json')[1].split('```')[0]
        elif '```' in response:
            response = response.split('```')[1].split('```')[0]
        
        try:
            parsed = json.loads(response.strip())
            if isinstance(parsed, dict) and 'decisions' in parsed:
                decisions = parsed.get('decisions') or {}
                cot_trace = parsed.get('cot_trace', [])
            else:
                decisions = parsed if isinstance(parsed, dict) else {}
                cot_trace = []

            if isinstance(cot_trace, str):
                cot_trace = [cot_trace]
            elif isinstance(cot_trace, dict):
                cot_trace = [json.dumps(cot_trace, ensure_ascii=False)]

            if (not cot_trace or (isinstance(cot_trace, list) and len(cot_trace) == 0)) and cot_segments:
                cot_trace = cot_segments

            return decisions, cot_trace
        except json.JSONDecodeError as e:
            print(f"[ERROR] JSON parse failed: {e}")
            print(f"[DATA] Response:\n{response}")
            return {}, cot_segments or []

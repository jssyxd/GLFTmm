# -*- coding: utf-8 -*-
"""
gateway_okx_client.py - Production-ready OKX Gateway Client
- Forces IPv4 address family to comply with IP-restricted API keys on dual-stack hosts.
- Full support for live trading and simulated trading.
- Includes real fill reconciliation (get_fills, get_order) to eliminate fake trade accounting.
"""

import socket
# Force IPv4 socket resolution globally for OKX requests
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(*args, **kwargs):
    responses = _orig_getaddrinfo(*args, **kwargs)
    ipv4_res = [r for r in responses if r[0] == socket.AF_INET]
    return ipv4_res if ipv4_res else responses
socket.getaddrinfo = _ipv4_getaddrinfo

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import urllib.error
import hmac
import hashlib
import base64
from typing import Dict, Any, List, Optional

class OKXGatewayClient:
    def __init__(self, api_key: str, api_secret: str, passphrase: str, simulated: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.simulated = simulated
        self.base_url = "https://www.okx.com"

    def _sign_and_request(self, method: str, endpoint: str, params: Optional[Dict] = None, body: Optional[Dict] = None) -> Dict[str, Any]:
        request_path = endpoint
        if params:
            query_str = urllib.parse.urlencode(params)
            request_path = f"{endpoint}?{query_str}"
        url = f"{self.base_url}{request_path}"

        ts = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
        body_str = json.dumps(body) if body else ""
        msg = f"{ts}{method}{request_path}{body_str}"
        sig = base64.b64encode(hmac.new(self.api_secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Content-Type': 'application/json',
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': sig,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
        }
        if self.simulated:
            headers['x-simulated-trading'] = '1'

        data_bytes = body_str.encode('utf-8') if body else None
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode('utf-8'))
                return err_data
            except Exception:
                return {"code": str(e.code), "msg": str(e.reason)}
        except Exception as e:
            return {"code": "-1", "msg": str(e)}

    def get_ticker(self, inst_id: str) -> Dict[str, Any]:
        res = self._sign_and_request('GET', '/api/v5/market/ticker', {'instId': inst_id})
        data = res.get('data', [])
        return data[0] if data else {}

    def get_books(self, inst_id: str, sz: int = 20) -> Dict[str, Any]:
        res = self._sign_and_request('GET', '/api/v5/market/books', {'instId': inst_id, 'sz': str(sz)})
        data = res.get('data', [])
        return data[0] if data else {}

    def get_candles(self, inst_id: str, bar: str = '15m', limit: int = 100) -> List[List[str]]:
        res = self._sign_and_request('GET', '/api/v5/market/candles', {'instId': inst_id, 'bar': bar, 'limit': limit})
        return res.get('data', [])

    def get_balance(self, ccy: str = 'USDT') -> Dict[str, Any]:
        res = self._sign_and_request('GET', '/api/v5/account/balance', {'ccy': ccy})
        data = res.get('data', [])
        if data:
            details = data[0].get('details', [])
            if details:
                return details[0]
            return data[0]
        return {}

    def get_positions(self, inst_type: str = 'SWAP', inst_id: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {'instType': inst_type}
        if inst_id:
            params['instId'] = inst_id
        res = self._sign_and_request('GET', '/api/v5/account/positions', params)
        return res.get('data', [])

    def place_order(
        self,
        inst_id: str,
        td_mode: str,
        side: str,
        ord_type: str,
        sz: str,
        px: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
        tag: Optional[str] = None
    ) -> Dict[str, Any]:
        body = {
            'instId': inst_id,
            'tdMode': td_mode,
            'side': side,
            'ordType': ord_type,
            'sz': sz
        }
        if px:
            body['px'] = px
        if cl_ord_id:
            body['clOrdId'] = cl_ord_id
        if tag:
            body['tag'] = tag
        return self._sign_and_request('POST', '/api/v5/trade/order', body=body)

    def cancel_order(self, inst_id: str, ord_id: Optional[str] = None, cl_ord_id: Optional[str] = None) -> Dict[str, Any]:
        body = {'instId': inst_id}
        if ord_id:
            body['ordId'] = ord_id
        if cl_ord_id:
            body['clOrdId'] = cl_ord_id
        return self._sign_and_request('POST', '/api/v5/trade/cancel-order', body=body)

    def cancel_batch_orders(self, orders: List[Dict[str, str]]) -> Dict[str, Any]:
        if not orders:
            return {"code": "0", "data": []}
        return self._sign_and_request('POST', '/api/v5/trade/cancel-batch-orders', body=orders[:20])

    def get_pending_orders(self, inst_type: str = 'SWAP', inst_id: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {'instType': inst_type}
        if inst_id:
            params['instId'] = inst_id
        res = self._sign_and_request('GET', '/api/v5/trade/orders-pending', params)
        return res.get('data', [])

    def get_order(self, inst_id: str, ord_id: Optional[str] = None, cl_ord_id: Optional[str] = None) -> Dict[str, Any]:
        """Query single order details to verify real fill state."""
        params = {'instId': inst_id}
        if ord_id:
            params['ordId'] = ord_id
        if cl_ord_id:
            params['clOrdId'] = cl_ord_id
        res = self._sign_and_request('GET', '/api/v5/trade/order', params)
        data = res.get('data', [])
        return data[0] if data else {}

    def get_fills(self, inst_type: str = 'SWAP', inst_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Query real trade fills history (only executed fills)."""
        params = {'instType': inst_type, 'limit': str(limit)}
        if inst_id:
            params['instId'] = inst_id
        res = self._sign_and_request('GET', '/api/v5/trade/fills', params)
        return res.get('data', [])

    def set_leverage(self, inst_id: str, lever: str, mgn_mode: str = "cross") -> Dict[str, Any]:
        return self._sign_and_request("POST", "/api/v5/account/set-leverage", body={"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode})

    def get_funding_rate(self, inst_id: str) -> Dict[str, Any]:
        res = self._sign_and_request("GET", "/api/v5/public/funding-rate", params={"instId": inst_id})
        data = res.get("data", [])
        return data[0] if data else {}

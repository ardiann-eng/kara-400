import asyncio
import json
import httpx

async def check_testnet_markets():
    url = "https://api.hyperliquid-testnet.xyz/info"
    payload = {"type": "metaAndAssetCtxs"}
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload)
        data = resp.json()
        
        universe = data[0].get("universe", [])
        contexts = data[1]
        
        active_markets = []
        for i, ctx in enumerate(contexts):
            if i < len(universe):
                vol = float(ctx.get("dayNtlVlm", 0))
                if vol > 0:
                    active_markets.append(universe[i]['name'])
        
        print(f"Total Universe: {len(universe)}")
        print(f"Active Markets (Vol > 0): {len(active_markets)}")
        print(f"Markets: {', '.join(active_markets)}")

if __name__ == "__main__":
    asyncio.run(check_testnet_markets())

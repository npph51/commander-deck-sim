"""One-off: the official decks' combos as Commander Spellbook lists them. Prints the answers, gzipped, as base64."""
import base64, gzip, json, time, urllib.request
out = {}
for d in json.load(open('official.json')):
    for attempt in range(3):
        try:
            req = urllib.request.Request('https://backend.commanderspellbook.com/find-my-combos', data=json.dumps(d['body']).encode(),
                                         headers={'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'deck-check/1.0'})
            raw = json.load(urllib.request.urlopen(req, timeout=60))
            r = raw.get('results', raw)
            out[d['name']] = {'results': {'included': r.get('included', []), 'almostIncluded': []}}
            print(d['name'], len(r.get('included', [])), flush=True)
            break
        except Exception as e:
            print(d['name'], 'failed', str(e)[:100], flush=True); time.sleep(5)
    time.sleep(1)
print('=== COMBOS gz64 ===')
b = base64.b64encode(gzip.compress(json.dumps(out).encode())).decode()
for i in range(0, len(b), 4000): print(b[i:i + 4000])
print('=== END COMBOS ===')

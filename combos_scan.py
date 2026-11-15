"""Which average decks hold which combos (Commander Spellbook), and which of those Forge's computer already knows.

    COMMANDERS='["Name", ...]' python3 -I combos_scan.py

For each commander: its EDHREC average deck from Forge's own collection, sent once to Commander Spellbook's public
find-my-combos (no key, a nameless User-Agent, one request a second), and every combo wholly in the deck kept with its
cards, what it gives, Spellbook's bracket tag and the mana it needs. A two-card combo is marked when Forge's own list of
Commander pairs (commander-bracket-combos.txt + ai-commander-combos.txt of the release tag) has it. Prints one JSON line
per deck between === SCAN === and === END SCAN ===. Nothing is kept here.
"""
import json, os, re, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import average_decks

API = 'https://backend.commanderspellbook.com/find-my-combos'
TAG = os.environ.get('FORGE_TAG', 'forge-2.0.16')

def key(n): return re.sub(r'[^a-z0-9]+', '', n.split(' // ')[0].lower())

def forge_pairs():
    pairs = set()
    for f in ('commander-bracket-combos.txt', 'ai-commander-combos.txt'):
        url = 'https://raw.githubusercontent.com/Card-Forge/forge/%s/forge-gui/res/lists/%s' % (TAG, f)
        try: text = urllib.request.urlopen(url, timeout=60).read().decode('utf-8')
        except Exception as e: print('no', f, e); continue
        for line in text.splitlines():
            parts = [p.strip() for p in line.split('|')]
            if line.startswith('#') or len(parts) < 3: continue
            pairs.add(frozenset((key(parts[1]), key(parts[2]))))
    return pairs

def cards_of(text):
    cmd, main, sec = [], [], None
    for line in text.replace('\r', '').split('\n'):
        s = line.strip()
        if s.startswith('['): sec = s.lower(); continue
        m = re.match(r'^(\d+)\s+([^|]+)', s)
        if not m: continue
        name = m.group(2).strip()
        if sec == '[commander]': cmd.append(name)
        elif sec == '[main]': main.append(name)
    return cmd, main

def ask(cmd, main):
    body = json.dumps({'commanders': [{'card': c, 'quantity': 1} for c in cmd], 'main': [{'card': c, 'quantity': 1} for c in sorted(set(main))]}).encode()
    req = urllib.request.Request(API, data=body, headers={'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'deck-check/1.0'})
    for attempt in range(3):
        try: return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as e:
            print('spellbook:', repr(e)[:120]); time.sleep(5)
    return None

def main():
    names = json.loads(os.environ.get('COMMANDERS') or '[]')
    pairs = forge_pairs()
    print('Forge knows %d Commander pairs' % len(pairs), flush=True)
    decks = average_decks(names)
    print('=== SCAN ===', flush=True)
    for n in names:
        text = decks.get(n) or ''
        if not text: print(json.dumps({'commander': n, 'deck': False}), flush=True); continue
        cmd, main = cards_of(text)
        raw = ask(cmd, main); time.sleep(1)
        if raw is None: print(json.dumps({'commander': n, 'error': True}), flush=True); continue
        r = raw.get('results', raw)
        have = {key(c) for c in cmd + main}
        out = []
        for v in r.get('included') or []:
            cards = [u['card']['name'] for u in v.get('uses') or [] if u.get('card')]
            if len(cards) < 2 or v.get('requires') or not all(key(c) in have for c in cards): continue
            out.append({'cards': cards, 'makes': sorted({p['feature']['name'] for p in v.get('produces') or [] if p.get('feature')})[:5],
                        'tag': v.get('bracketTag'), 'mv': v.get('manaValueNeeded'), 'pop': v.get('popularity'),
                        'forge_knows': len(cards) == 2 and frozenset(key(c) for c in cards) in pairs})
        print(json.dumps({'commander': n, 'cards': len(main) + len(cmd), 'combos': out}), flush=True)
    print('=== END SCAN ===', flush=True)

if __name__ == '__main__': main()

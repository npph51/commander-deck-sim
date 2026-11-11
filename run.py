"""Commander deck test.

Plays four-player Commander games AI against AI with Forge's own `sim` mode (github.com/Card-Forge/forge, GPL; this
repository only runs Forge's release, none of its code is here) and prints one JSON summary: the deck under test
against three official preconstructed decks (mode test), or the precons among themselves (mode calib).
"""
import json, os, random, re, subprocess, sys, time, glob, math

HERE = os.path.dirname(os.path.abspath(__file__))
FORGE = os.environ.get('FORGE_DIR', os.path.join(os.getcwd(), 'forge'))
MODE = os.environ.get('MODE', 'smoke')          # smoke | test | calib
STAGE = os.environ.get('STAGE', 'all')           # play (one machine's share of the tables) | merge (all shares into one summary) | all
SHARD = int(os.environ.get('SHARD', '0')); SHARDS = max(1, int(os.environ.get('SHARDS', '1')))   # this machine of how many
DECK = 'deck.dck'                                  # the deck under test, written from DECKLIST
DECKLIST = os.environ.get('DECKLIST', '')          # its list in Forge's .dck format, passed in with the run (never committed)
PODS = min(200, int(os.environ.get('PODS', '2')))          # tables of four; each is one JVM start
GAMES = min(20, int(os.environ.get('GAMES', '3')))        # games per table
CLOCK = int(os.environ.get('CLOCK', '300'))      # seconds before Forge calls a game a draw
SEED = int(os.environ.get('SEED', '1'))
PROFILE = os.environ.get('PROFILE', '')          # Forge AI profile for every seat ('' = its default)
# The benchmark: twelve ordinary Wizards precons of 2023-24 (no premium lines), as Forge ships their lists.
PRECONS = ['BLC - Animated Army', 'BLC - Family Matters', 'BLC - Peace Offering', 'BLC - Squirreled Away',
           'DSC - Death Toll', 'DSC - Endless Punishment', 'DSC - Jump Scare!', 'DSC - Miracle Worker',
           'LCC - Ahoy Mateys', 'LCC - Blood Rites', 'LCC - Explorers of the Deep', 'LCC - Veloci-Ramp-Tor']

COMMANDER = None   # the tested deck's commander, read from its file

def slug(s): return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')

def prepare():
    work = os.path.join(os.getcwd(), 'simdecks')
    os.makedirs(work, exist_ok=True)
    src = os.path.join(FORGE, 'res', 'adventure', 'Realm of Legends', 'decks', 'starter', 'commanderprecon')
    lib = {}
    for p in PRECONS:
        f = os.path.join(src, p + '.dck')
        if not os.path.exists(f):
            print('precon missing in this Forge release:', p); continue
        out = 'p_' + slug(p) + '.dck'
        txt = open(f, encoding='utf-8').read()
        # One plain name per deck, so the winner line names it without brackets.
        txt = re.sub(r'(?m)^Name=.*$', 'Name=' + p.replace(' - ', ' ').replace('!', ''), txt, count=1)
        open(os.path.join(work, out), 'w', encoding='utf-8').write(txt)
        lib[out] = p
    if MODE != 'calib':
        text = DECKLIST.replace('\r', '')
        if '\\n' in text and '\n' not in text: text = text.replace('\\n', '\n')
        if len(text) > 20000 or not re.search(r'(?i)\[main\]', text): sys.exit('no usable decklist was passed in')
        open(os.path.join(work, DECK), 'w', encoding='utf-8').write(text)
    global COMMANDER
    mine = os.path.join(work, os.path.basename(DECK))
    if not os.path.exists(mine): return work, lib
    m = re.search(r'(?is)\[commander\]\s*\n1 ([^|\n]+)', open(mine, encoding='utf-8').read())
    COMMANDER = m.group(1).strip().rstrip('+') if m else None
    return work, lib

def jar():
    js = glob.glob(os.path.join(FORGE, 'forge-gui-desktop-*-jar-with-dependencies.jar'))
    if not js: sys.exit('no Forge jar in ' + FORGE + ': ' + ' '.join(os.listdir(FORGE))[:400])
    return js[0]

def play(work, decks, games, quiet=True):
    java = os.path.join(os.environ.get('JAVA_HOME_21_X64') or os.environ.get('JAVA_HOME') or '', 'bin', 'java')
    if not os.path.exists(java): java = 'java'
    cmd = ['xvfb-run', '-a', java, '-Xmx2600m', '-Djava.awt.headless=false', '-jar', jar(), 'sim', '-d', *decks,
           '-D', work + os.sep, '-f', 'commander', '-n', str(games), '-c', str(CLOCK)]
    if PROFILE: cmd += ['-a', *([PROFILE] * len(decks))]
    if quiet: cmd.append('-q')
    t = time.time()
    r = subprocess.run(cmd, cwd=FORGE, capture_output=True, text=True, timeout=games * (CLOCK + 60) + 600)
    return r.stdout + '\n' + r.stderr[-3000:], time.time() - t

RES = re.compile(r'Game Result: Game (\d+) ended in (\d+) ms\. (.*) has won!')
DRAW = re.compile(r'Game Result: Game (\d+) ended in a Draw! Took (\d+) ms')

TURN = re.compile(r'^Turn: Turn (\d+) \(Ai\((\d+)\)-')
ACT = re.compile(r'^(Land|Add To Stack): Ai\((\d+)\)-.*? (played|cast) (.+?)(?: \(\d+\))?(?: targeting .*)?$')
LOST = re.compile(r'^Game Outcome: Ai\((\d+)\)-.* has lost')

def parse(out, decks, names):
    """One record per game from Forge's full log: winner, turns, and for our deck (seat `me`) what it did."""
    me = decks.index(DECK) + 1 if DECK in decks else 0
    games = []; cur = None
    def fresh(): return {'me': me, 'turns': 0, 'my_turns': 0, 'cast': {}, 'first': {}, 'lands_by_t6': 0, 'cmd_turn': None, 'lost_order': [], 'timeout': False}
    cur = fresh()
    for line in out.splitlines():
        m = TURN.match(line)
        if m:
            cur['turns'] = int(m.group(1))
            if int(m.group(2)) == me: cur['my_turns'] += 1
            continue
        m = ACT.match(line)
        if m and int(m.group(2)) == me:
            card = m.group(4).strip()
            cur['cast'][card] = cur['cast'].get(card, 0) + 1
            cur['first'].setdefault(card, cur['my_turns'])
            if m.group(1) == 'Land' and cur['my_turns'] <= 6: cur['lands_by_t6'] += 1
            if card == COMMANDER and cur['cmd_turn'] is None: cur['cmd_turn'] = cur['my_turns']
            continue
        m = LOST.match(line)
        if m: cur['lost_order'].append(int(m.group(1))); continue
        if 'Stopping slow match as draw' in line: cur['timeout'] = True; continue
        m = RES.search(line)
        if m:
            w = re.match(r'Ai\((\d+)\)-', m.group(3))
            cur.update(ms=int(m.group(2)), winner=decks[int(w.group(1)) - 1] if w else m.group(3), me_won=bool(w and int(w.group(1)) == me))
            games.append(cur); cur = fresh(); continue
        m = DRAW.search(line)
        if m: cur.update(ms=int(m.group(2)), winner=None, me_won=False); games.append(cur); cur = fresh()
    return games

def wilson(k, n, z=1.96):
    if not n: return [0, 0]
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * (c - h), 1), round(100 * (c + h), 1)]

def tables(lib):
    rnd = random.Random(SEED)
    pods = []
    for _ in range(PODS):
        if MODE == 'calib': ds = rnd.sample(sorted(lib), 4)
        else: ds = [DECK] + rnd.sample(sorted(lib), 3)
        rnd.shuffle(ds)
        pods.append(ds)
    return pods

def play_stage(work, lib):
    """This machine's tables (every SHARDS-th one), each game's record written to records/<shard>.json."""
    pods = [ds for i, ds in enumerate(tables(lib)) if i % SHARDS == SHARD]
    if MODE == 'smoke':
        out, secs = play(work, pods[0], 1, quiet=False)
        lines = out.splitlines()
        print('=== SMOKE: one game with its full log, %d lines, %.0f s ===' % (len(lines), secs))
        print('\n'.join(lines[:60])); print('...'); print('\n'.join(lines[-120:]))
    t0 = time.time(); recs = []; bad = []; n = 0
    for ds in pods:
        try: out, secs = play(work, ds, GAMES, quiet=False)
        except subprocess.TimeoutExpired: bad.append({'decks': ds, 'games_read': 0, 'tail': 'table timed out'}); continue
        gs = parse(out, ds, lib)
        if len(gs) < GAMES: bad.append({'decks': ds, 'games_read': len(gs), 'tail': out[-800:]})
        for g in gs:
            n += 1; g['decks'] = ds; recs.append(g)
            print('game', n, [lib.get(d, d) for d in ds], '->', lib.get(g['winner'], g['winner']), 'turn', g['turns'], round(g['ms'] / 1000), 's', 'TIMEOUT' if g['timeout'] else '', flush=True)
    os.makedirs('records', exist_ok=True)
    json.dump({'shard': SHARD, 'minutes': round((time.time() - t0) / 60, 1), 'games': recs, 'bad': bad}, open('records/%d.json' % SHARD, 'w'))

def card_results(mine, deck_cards):
    """Per card of our deck: [name, games it was played in, of which won, the turn of ours it usually first came down,
    comparable games without it, of which won]. A card is only played in games that last long enough for it, and long
    games are won more often, so the games without it are only those in which our deck had at least that many turns."""
    rows = []
    for c in deck_cards:
        played = [g for g in mine if c in g['cast']]
        turns = sorted(g.get('first', {}).get(c) or 0 for g in played)
        t = turns[len(turns) // 2] if turns else None
        rest = [g for g in mine if c not in g['cast'] and g['my_turns'] >= (t or 0)]
        rows.append([c, len(played), sum(g['me_won'] for g in played), t, len(rest), sum(g['me_won'] for g in rest)])
    return rows

def merge_stage(work, lib):
    parts = [json.load(open(f)) for f in sorted(glob.glob('records/**/*.json', recursive=True))]
    stats = {}; total = draws = 0; ms = []; mine = []; casts = {}
    bad = [b for p in parts for b in p['bad']]
    for g in (g for p in parts for g in p['games']):
        ds = g['decks']; total += 1; ms.append(g['ms'])
        if DECK in ds:
            mine.append(g)
            for c in g['cast']: casts[c] = casts.get(c, 0) + 1
        if g['winner'] is None: draws += 1
        for d in ds:
            s = stats.setdefault(d, {'games': 0, 'wins': 0}); s['games'] += 1
            if g['winner'] == d: s['wins'] += 1
    rows = [{'deck': lib.get(d, d), 'games': s['games'], 'wins': s['wins'], 'win_pct': round(100 * s['wins'] / s['games'], 1),
             'ci95': wilson(s['wins'], s['games'])} for d, s in sorted(stats.items(), key=lambda x: -x[1]['wins'] / max(1, x[1]['games']))]
    ms.sort()
    summary = {'mode': MODE, 'deck': DECK, 'pods': PODS, 'games_per_pod': GAMES, 'clock_s': CLOCK, 'profile': PROFILE or 'default',
               'machines': len(parts), 'machines_expected': SHARDS, 'games': total, 'games_expected': PODS * GAMES, 'draws_or_timeouts': draws,
               'machine_minutes': [p['minutes'] for p in parts],
               'game_seconds_median': round(ms[len(ms) // 2] / 1000, 1) if ms else None,
               'game_seconds_p90': round(ms[int(len(ms) * .9)] / 1000, 1) if ms else None, 'decks': rows, 'incomplete_pods': bad[:5]}
    if mine:
        n = len(mine); med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
        deck_cards = [re.sub(r'^\d+ ', '', l).split('|')[0].strip() for l in open(os.path.join(work, DECK), encoding='utf-8').read().split('[Main]')[-1].splitlines() if re.match(r'^1 ', l)]
        summary['our_deck'] = {
            'games': n, 'wins': sum(g['me_won'] for g in mine), 'turns_median': med([g['turns'] for g in mine]),
            'lands_by_our_turn_6_avg': round(sum(g['lands_by_t6'] for g in mine) / n, 2),
            'commander_cast_pct': round(100 * sum(g['cmd_turn'] is not None for g in mine) / n, 1),
            'commander_turn_median': med([g['cmd_turn'] for g in mine if g['cmd_turn'] is not None]),
            'first_out_pct': round(100 * sum(1 for g in mine if g['lost_order'][:1] == [g['me']]) / n, 1),
            'timeouts': sum(g['timeout'] for g in mine),
            # Share of games in which each card was played at least once; the lowest are the candidates to swap.
            'least_played': sorted(([c, round(100 * casts.get(c, 0) / n, 1)] for c in deck_cards), key=lambda x: x[1])[:20],
            'most_played': sorted(([c, round(100 * casts.get(c, 0) / n, 1)] for c in deck_cards), key=lambda x: -x[1])[:10],
            'cards': card_results(mine, deck_cards),
        }
    print('=== DECK SIM SUMMARY ==='); print(json.dumps(summary, indent=1)); print('=== END SUMMARY ===')

def main():
    work, lib = prepare()
    if STAGE in ('play', 'all'): play_stage(work, lib)
    if STAGE in ('merge', 'all'): merge_stage(work, lib)

main()

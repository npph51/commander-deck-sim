"""Commander deck test.

Plays four-player Commander games AI against AI with Forge's own `sim` mode (github.com/Card-Forge/forge, GPL; this
repository only runs Forge's release, none of its code is here) and prints one JSON summary: the deck under test
against three official preconstructed decks (mode test), the precons among themselves (mode calib), or the premium
precons and players' average decks each against three ordinary precons (mode ladder).
"""
import json, os, random, re, signal, subprocess, sys, threading, time, glob, math

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
WORKERS = max(1, min(8, int(os.environ.get('WORKERS') or 1)))   # tables played side by side on one machine (one JVM each)
# A machine's time budget in minutes (0 = none): past it no new table is started, and GRACE minutes later the tables
# still playing are stopped and hand in the games they finished, so one stalled table never holds the whole run up.
BUDGET = max(0, int(os.environ.get('BUDGET') or 0)); GRACE = 10
SKIP = max(0, min(2000, int(os.environ.get('SKIP') or 0)))   # tables of the seeded deal to pass over: a second part plays the next ones
PROFILE = os.environ.get('PROFILE', '')          # Forge AI profile for every seat ('' = its default)
# The benchmarks, as Forge ships the lists. "standard": the ordinary Wizards precons of late 2023 to 2026 (no premium
# lines). "strong": the premium lines, built above an ordinary precon, for decks meant to play above that level.
# "classic": the first twelve, kept so an old result can be played again. "all" (calib only): standard and strong together.
POOLS = {
    'classic': ['BLC - Animated Army', 'BLC - Family Matters', 'BLC - Peace Offering', 'BLC - Squirreled Away',
                'DSC - Death Toll', 'DSC - Endless Punishment', 'DSC - Jump Scare!', 'DSC - Miracle Worker',
                'LCC - Ahoy Mateys', 'LCC - Blood Rites', 'LCC - Explorers of the Deep', 'LCC - Veloci-Ramp-Tor'],
    'strong': ['M3C - Creative Energy', 'M3C - Eldrazi Incursion', 'M3C - Graveyard Overdrive', 'M3C - Tricky Terrain',
               '40K - Forces of the Imperium', '40K - Necron Dynasties', '40K - The Ruinous Powers', '40K - Tyranid Swarm',
               'PIP - Hail, Caesar', 'PIP - Mutant Menace', 'PIP - Science!', 'PIP - Scrappy Survivors'],
}
POOLS['standard'] = POOLS['classic'] + [
    'DRC - Eternal Might', 'DRC - Living Energy',
    'TDC - Abzan Armor', 'TDC - Jeskai Striker', 'TDC - Mardu Surge', 'TDC - Sultai Arisen', 'TDC - Temur Roar',
    'EOC - Counter Intelligence', 'EOC - World Shaper',
    'ECC - Blight Curse', 'ECC - Dance of the Elements',
    'SOC - Lorehold Spirit', 'SOC - Prismari Artistry', 'SOC - Quandrix Unlimited', 'SOC - Silverquill Influence', 'SOC - Witherbloom Pestilence']
POOLS['all'] = POOLS['standard'] + POOLS['strong']
POOL = os.environ.get('POOL') or 'classic'
if POOL not in POOLS: POOL = 'classic'
PRECONS = POOLS['all'] if os.environ.get('MODE') == 'ladder' else POOLS[POOL]

# Player-built lists as Forge offers them for download: one "average" deck per commander (what players most often run
# with it). AVERAGE (mode test): that commander's average deck is the deck under test, in place of a list passed in.
# EXTRA (mode ladder): commanders whose average decks sit down as subjects, names separated by |.
AVERAGE = (os.environ.get('AVERAGE') or '').strip()
EXTRA = [x.strip() for x in (os.environ.get('EXTRA') or '').split('|') if x.strip()]
AVERAGES_URL = 'https://github.com/Card-Forge/forge-extras/raw/refs/heads/main/decks/edhrec-average.zip'

def average_decks(names):
    """name -> the deck's text ('' when the collection has none for it), under one plain deck name each."""
    import io, urllib.request, zipfile
    f = 'averages.zip'
    if not os.path.exists(f):
        for attempt in range(3):
            try:
                urllib.request.urlretrieve(AVERAGES_URL, f); break
            except Exception as e:
                print('average decks: download failed,', e); time.sleep(5)
    if not os.path.exists(f): return {n: '' for n in names}
    z = zipfile.ZipFile(f)
    key = lambda x: re.sub(r'[^a-z0-9]+', '', x.lower())
    have = {key(os.path.basename(i)[:-4]): i for i in z.namelist() if i.endswith('.dck')}
    out = {}
    for n in names:
        i = have.get(key(n)) or have.get(key(n.split(' // ')[0]))
        out[n] = z.read(i).decode('utf-8', 'replace') if i else ''
    return out

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
    if MODE == 'ladder':
        for n, text in average_decks(EXTRA).items():
            if not text:
                print('no average deck for:', n); continue
            out = 'a_' + slug(n) + '.dck'
            text = re.sub(r'(?m)^Name=.*$', 'Name=Average ' + re.sub(r'[^A-Za-z0-9 ]+', '', n), text, count=1)
            open(os.path.join(work, out), 'w', encoding='utf-8').write(text)
            lib[out] = 'AVG - ' + n
    elif MODE != 'calib':
        text = DECKLIST.replace('\r', '')
        if AVERAGE:
            text = re.sub(r'(?m)^Name=.*$', 'Name=Deck', average_decks([AVERAGE])[AVERAGE], count=1)
            if not text: print('=== NO AVERAGE DECK FOR THIS COMMANDER ===')
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

def play(work, decks, games, quiet=True, slot=0, limit=None):
    java = os.path.join(os.environ.get('JAVA_HOME_21_X64') or os.environ.get('JAVA_HOME') or '', 'bin', 'java')
    if not os.path.exists(java): java = 'java'
    cmd = ['xvfb-run', '-a', java, '-Xmx2600m', '-Djava.awt.headless=false', '-jar', jar(), 'sim', '-d', *decks,
           '-D', work + os.sep, '-f', 'commander', '-n', str(games), '-c', str(CLOCK)]
    if PROFILE: cmd += ['-a', *([PROFILE] * len(decks))]
    if quiet: cmd.append('-q')
    t = time.time()
    # Tables side by side each get a home of their own, so two JVMs never write the same preference or cache files.
    env = dict(os.environ)
    if WORKERS > 1:
        home = os.path.join(os.getcwd(), 'homes', str(slot)); os.makedirs(home, exist_ok=True)
        env['HOME'] = home
        cmd = cmd[:3] + ['-Duser.home=' + home] + cmd[3:]
    # Its own process group, so a table stopped at the limit takes its JVM with it and what it printed so far is kept.
    most = games * (CLOCK + 60) + 600
    p = subprocess.Popen(cmd, cwd=FORGE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    try: out, err = p.communicate(timeout=most if limit is None else max(30, min(most, limit)))
    except subprocess.TimeoutExpired:
        try: os.killpg(p.pid, signal.SIGKILL)
        except OSError: pass
        out, err = p.communicate()
        return (out or '') + '\n=== TABLE STOPPED AT THE TIME LIMIT ===\n', time.time() - t
    return out + '\n' + err[-3000:], time.time() - t

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
    # ladder: every premium precon and every average deck in turn against three ordinary precons, as a deck under test sits.
    field = sorted(d for d, n in lib.items() if n in POOLS['standard'])
    subjects = sorted(d for d in lib if d not in field)
    for i in range(SKIP + PODS):
        if MODE == 'ladder': ds = [subjects[i % len(subjects)]] + rnd.sample(field, 3)
        elif MODE == 'calib': ds = rnd.sample(sorted(lib), 4)
        else: ds = [DECK] + rnd.sample(sorted(lib), 3)
        rnd.shuffle(ds)
        pods.append(ds)
    return pods[SKIP:]

def play_stage(work, lib):
    """This machine's tables (every SHARDS-th one), each game's record written to records/<shard>.json."""
    pods = [ds for i, ds in enumerate(tables(lib)) if i % SHARDS == SHARD]
    if MODE == 'smoke':
        out, secs = play(work, pods[0], 1, quiet=False)
        lines = out.splitlines()
        print('=== SMOKE: one game with its full log, %d lines, %.0f s ===' % (len(lines), secs))
        print('\n'.join(lines[:60])); print('...'); print('\n'.join(lines[-120:]))
    t0 = time.time(); recs = []; bad = []; skipped = []
    from concurrent.futures import ThreadPoolExecutor
    import queue
    slots = queue.Queue(); lock = threading.Lock()
    for k in range(WORKERS): slots.put(k)
    os.makedirs('records', exist_ok=True)
    def save():
        # After every table, so a machine that is cut off still hands in what it played.
        tmp = 'records/%d.json.tmp' % SHARD
        json.dump({'shard': SHARD, 'minutes': round((time.time() - t0) / 60, 1), 'games': recs, 'bad': bad, 'skipped': len(skipped)}, open(tmp, 'w'))
        os.replace(tmp, 'records/%d.json' % SHARD)
    def one(ds):
        k = slots.get()
        try:
            left = (BUDGET + GRACE) * 60 - (time.time() - t0) if BUDGET else None
            if BUDGET and time.time() - t0 > BUDGET * 60:
                with lock: skipped.append(ds); save()
                return
            out = play(work, ds, GAMES, quiet=False, slot=k, limit=left)[0]
            gs = parse(out, ds, lib)
            with lock:
                if len(gs) < GAMES: bad.append({'decks': ds, 'games_read': len(gs), 'tail': out[-800:]})
                for g in gs:
                    g['decks'] = ds; recs.append(g)
                    print('game', len(recs), [lib.get(d, d) for d in ds], '->', lib.get(g['winner'], g['winner']), 'turn', g['turns'], round(g['ms'] / 1000), 's', 'TIMEOUT' if g['timeout'] else '', flush=True)
                save()
        finally: slots.put(k)
    with ThreadPoolExecutor(WORKERS) as ex: list(ex.map(one, pods))
    with lock: save()
    print('machine %d: %d games of %d tables in %.0f min, %d tables not started (time budget), %d cut short' % (SHARD, len(recs), len(pods), (time.time() - t0) / 60, len(skipped), len(bad)), flush=True)

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
    if MODE == 'ladder':
        # Per group: the subjects' share of their own tables' games (an even deck wins a quarter).
        for r in rows: r['group'] = 'average' if r['deck'].startswith('AVG - ') else 'strong' if r['deck'] in POOLS['strong'] else 'standard'
        groups = {}
        for r in rows:
            g = groups.setdefault(r['group'], {'decks': 0, 'games': 0, 'wins': 0}); g['decks'] += 1; g['games'] += r['games']; g['wins'] += r['wins']
        for g in groups.values(): g['win_pct'] = round(100 * g['wins'] / max(1, g['games']), 1); g['ci95'] = wilson(g['wins'], g['games'])
    summary = {'mode': MODE, 'pool': POOL, 'deck': DECK, 'pods': PODS, 'games_per_pod': GAMES, 'clock_s': CLOCK, 'profile': PROFILE or 'default',
               'machines': len(parts), 'machines_expected': SHARDS, 'workers': WORKERS, 'games': total, 'games_expected': PODS * GAMES, 'draws_or_timeouts': draws,
               'machine_minutes': [p['minutes'] for p in parts], 'budget_min': BUDGET, 'skip': SKIP,
               'tables_not_started': sum(p.get('skipped', 0) for p in parts),
               'game_seconds_median': round(ms[len(ms) // 2] / 1000, 1) if ms else None,
               'game_seconds_p90': round(ms[int(len(ms) * .9)] / 1000, 1) if ms else None, 'decks': rows, 'groups': groups if MODE == 'ladder' else None, 'average': AVERAGE or None, 'incomplete_pods': bad[:5]}
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

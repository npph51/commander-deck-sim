"""Commander deck test.

Plays four-player Commander games AI against AI with Forge's own `sim` mode (github.com/Card-Forge/forge, GPL; this
repository only runs Forge's release, none of its code is here; see log_more for the one file compiled at run time) and prints one JSON summary: the deck under test
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
MC = os.environ.get('MC', '')                    # Forge's Flat Monte Carlo layer: on, max or off ('' = Forge's default in simulations, off)
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
# "bracket2": the standard pool without the decks that are above Commander Bracket 2 by Wizards' own requirements as
# the lists stand (a Game Changer, mass land denial, a turn-chaining card, or a two-card game-deciding combo under eight
# mana as Commander Spellbook lists it), so a deck built for Bracket 2 only meets Bracket 2 decks.
ABOVE_BRACKET_2 = ['BLC - Family Matters',     # Combat Celebrant + Helm of the Host
                   'LCC - Ahoy Mateys',        # Timestream Navigator
                   'DRC - Living Energy',      # Lightning Runner + Aetherwind Basker
                   'TDC - Abzan Armor',        # Seedborn Muse
                   'EOC - World Shaper']       # Planetary Annihilation
POOLS['bracket2'] = [d for d in POOLS['standard'] if d not in ABOVE_BRACKET_2]
POOLS['all'] = POOLS['standard'] + POOLS['strong']
POOL = os.environ.get('POOL') or 'classic'
if POOL not in POOLS: POOL = 'classic'
PRECONS = POOLS['all'] if os.environ.get('MODE') == 'ladder' else POOLS[POOL]

# Player-built lists as Forge offers them for download: one "average" deck per commander (what players most often run
# with it). AVERAGE (mode test): that commander's average deck is the deck under test, in place of a list passed in.
# EXTRA (mode ladder): commanders whose average decks sit down as subjects, names separated by |.
# A trial (mode test): card swaps to try side by side, as JSON [[card out, card in], ...] in the deck file's own names
# (at most 16). Each table plays the deck with a random half of them made (every swap at half the tables, seeded), and
# the summary says how the deck finished per combination, so each swap's own effect can be worked out from one run.
try: SWAPS = [[str(a), str(b)] for a, b in json.loads(os.environ.get('SWAPS') or '[]')][:16]
except Exception: SWAPS = []
# Every table plays on its own fixed random seed (from SEED and the table's place in the deal), so the same table of
# another run of this deck starts from the same shuffles: two versions of a list then meet the same opening hands at
# the same tables, which takes luck out of the comparison. '0' = Forge's own random seed, as before.
FSEED = os.environ.get('FSEED', '1') != '0'
def table_seed(i): return 1 + (SEED * 100003 + i * 7919) % 2000000000
# Every game of a table on a seed of its own (one Forge start per game), so the same game of another run of the deck
# is dealt the same way: lists are then compared game by game. Game 0 keeps the table's seed, as runs before had it.
PERGAME = FSEED and os.environ.get('PERGAME', '1') != '0'
def game_seed(base, g): return base if not g else 1 + (base * 31 + g * 104729) % 2000000000
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

def java_bin(name='java'):
    j = os.path.join(os.environ.get('JAVA_HOME_21_X64') or os.environ.get('JAVA_HOME') or '', 'bin', name)
    return j if os.path.exists(j) else name

# Forge's game log leaves out which cards a player draws (it has the event, its log writer drops it) and who controls
# the card that dealt damage. log_more compiles that one file of the release's own source with both added, at run time
# (nothing of Forge is kept here), and puts it in front of the jar. Any failure = the release as it is, without them.
LOGMORE = None   # [java flags, class path, main class] once it compiled
DRAW_JAVA = """if (ev.card() != null && ((from == ZoneType.Library && to == ZoneType.Hand) || (from == ZoneType.Hand && to == ZoneType.Library))) {
            return new GameLogEntry(GameLogEntryType.INFORMATION, (to == ZoneType.Hand ? "DRAW|" : "BACK|") + ev.card().getOwner() + "|" + ev.card().getName());
        }
        """
DRAW_AT = 'if (from != ZoneType.Battlefield || (to != ZoneType.Graveyard && to != ZoneType.Exile)) {'
DMG_OLD = 'return new GameLogEntry(GameLogEntryType.DAMAGE, message, ev.source());'
DMG_NEW = 'return new GameLogEntry(GameLogEntryType.DAMAGE, message + " {by|" + (ev.source() == null ? "|" : ev.source().getController() + "|" + ev.source().getName()) + "|" + ev.amount() + "|" + ev.target() + "}", ev.source());'

def log_more():
    """Tries the fuller log once; says in the run's log what happened."""
    global LOGMORE
    import urllib.request, zipfile
    try:
        j = jar()
        v = re.search(r'forge-gui-desktop-(.+?)-jar-with-dependencies', os.path.basename(j)).group(1)
        url = 'https://raw.githubusercontent.com/Card-Forge/forge/forge-%s/forge-game/src/main/java/forge/game/GameLogFormatter.java' % v
        src = urllib.request.urlopen(url, timeout=60).read().decode('utf-8')
        if src.count(DRAW_AT) != 1: raise RuntimeError('the zone-change code is not as expected')
        man = zipfile.ZipFile(j).read('META-INF/MANIFEST.MF').decode('utf-8', 'replace').replace('\r', '').replace('\n ', '')
        main = re.search(r'(?m)^Main-Class: *(\S+)', man).group(1)
        opens = re.search(r'(?m)^Add-Opens: *(.*)$', man)
        flags = [x for o in (opens.group(1).split() if opens else []) for x in ('--add-opens', o + '=ALL-UNNAMED')]
        base = os.path.join(os.getcwd(), 'logmore')
        for k, with_damage in enumerate((True, False)):
            text = src.replace(DRAW_AT, DRAW_JAVA + DRAW_AT)
            if with_damage:
                if text.count(DMG_OLD) != 1: continue
                text = text.replace(DMG_OLD, DMG_NEW)
            d = os.path.join(base, str(k)); os.makedirs(os.path.join(d, 'src'), exist_ok=True)
            f = os.path.join(d, 'src', 'GameLogFormatter.java'); open(f, 'w', encoding='utf-8').write(text)
            r = subprocess.run([java_bin('javac'), '-nowarn', '-cp', j, '-d', os.path.join(d, 'classes'), f], capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                LOGMORE = [flags, os.path.join(d, 'classes') + os.pathsep + j, main]
                print('fuller log: draws%s (Forge %s)' % (' and damage by player' if with_damage else '', v), flush=True); return
            print('fuller log: did not compile:', r.stderr[-600:], flush=True)
    except Exception as e:
        print('fuller log: not available,', repr(e)[:300], flush=True)

def play(work, decks, games, quiet=True, slot=0, limit=None, seed=None):
    java = java_bin()
    run = [*LOGMORE[0], '-cp', LOGMORE[1], LOGMORE[2]] if LOGMORE else ['-jar', jar()]
    cmd = ['xvfb-run', '-a', java, '-Xmx2600m', '-Djava.awt.headless=false', *run, 'sim', '-d', *decks,
           '-D', work + os.sep, '-f', 'commander', '-n', str(games), '-c', str(CLOCK)]
    if seed: cmd += ['-s', str(seed)]
    if PROFILE: cmd += ['-a', *([PROFILE] * len(decks))]
    if MC in ('on', 'max', 'off'): cmd += ['-mc', MC]
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
# Only with the fuller log (log_more): a card to or from a hand, and damage with the player whose card dealt it.
MOVE = re.compile(r'^[^:]+: (DRAW|BACK)\|Ai\((\d+)\)-[^|]*\|(.*)$')
DMG = re.compile(r'\{by\|(?:Ai\((\d+)\)-[^|]*)?\|([^|]*)\|(\d+)\|Ai\((\d+)\)-.*\}$')
MULL = re.compile(r'^Mulligan: .*Ai\((\d+)\)-.* mulliganed ')   # not the line for a hand that was kept
TOP = 6   # draws after the opening hand that still count as the top of the shuffled deck

def parse(out, decks, names, mine=DECK):
    """One record per game from Forge's full log: winner, turns, and for our deck (seat `me`) what it did."""
    me = decks.index(mine) + 1 if mine in decks else 0
    games = []; cur = None
    def fresh(): return {'me': me, 'turns': 0, 'my_turns': 0, 'cast': {}, 'first': {}, 'lands_by_t6': 0, 'cmd_turn': None, 'early': 0, 'lost_order': [], 'timeout': False,
                          'open': [], 'top': [], 'mull': 0, 'dl': False, 'dealt': {}, 'hit': 0, 'hit6': 0}
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
            if m.group(1) != 'Land' and cur['my_turns'] <= 4: cur['early'] += 1
            if card == COMMANDER and cur['cmd_turn'] is None: cur['cmd_turn'] = cur['my_turns']
            continue
        m = MOVE.match(line)
        if m:
            cur['dl'] = True
            if int(m.group(2)) == me:
                card = m.group(3).strip()
                if cur['turns'] == 0:
                    # Before the first turn: the opening hand, mulligans included (a hand put back leaves it again).
                    if m.group(1) == 'DRAW': cur['open'].append(card)
                    elif card in cur['open']: cur['open'].remove(card)
                elif m.group(1) == 'DRAW' and len(cur['top']) < TOP: cur['top'].append(card)
            continue
        m = MULL.match(line)
        if m:
            if int(m.group(1)) == me: cur['mull'] += 1
            continue
        if line.startswith('Damage:'):
            m = DMG.search(line)
            if m:
                by = int(m.group(1) or 0); to = int(m.group(4)); n = min(60, int(m.group(3)))   # one runaway hit says no more than a lethal one
                if to == me:
                    cur['hit'] += n
                    if cur['my_turns'] <= 6: cur['hit6'] += n
                elif by == me and m.group(2): cur['dealt'][m.group(2)] = cur['dealt'].get(m.group(2), 0) + n
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

def masks(n):
    """Which swaps each of this run's n tables plays with: bit j = swap j made. Every swap at half the tables."""
    out = [0] * n
    for j in range(len(SWAPS)):
        r = random.Random('%d-%d-%d' % (SEED, SKIP, j))
        for i in r.sample(range(n), n // 2): out[i] |= 1 << j
    return out

def swapped(work, mask):
    """The deck file for a table that plays with these swaps made (written once per combination)."""
    if not mask: return DECK
    name = 'deck_m%d.dck' % mask
    f = os.path.join(work, name)
    if not os.path.exists(f):
        lines = open(os.path.join(work, DECK), encoding='utf-8').read().split('\n')
        for j, (out, into) in enumerate(SWAPS):
            if not mask >> j & 1: continue
            for k, l in enumerate(lines):
                if l.strip() == '1 ' + out: lines[k] = '1 ' + into; break
        open(f, 'w', encoding='utf-8').write('\n'.join(lines))
    return name

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
    every = tables(lib); ms = masks(len(every)) if SWAPS and MODE == 'test' else [0] * len(every)
    mask_of = {id(ds): m for ds, m in zip(every, ms)}
    seed_of = {id(ds): table_seed(SKIP + i) for i, ds in enumerate(every)} if FSEED else {}
    index_of = {id(ds): SKIP + i for i, ds in enumerate(every)}
    pods = [ds for i, ds in enumerate(every) if i % SHARDS == SHARD]
    if MODE != 'calib' and MODE != 'ladder' and os.environ.get('LOGMORE', '1') != '0': log_more()
    if MODE == 'smoke':
        out, secs = play(work, pods[0], 1, quiet=False)
        lines = out.splitlines()
        print('=== SMOKE: one game with its full log, %d lines, %.0f s ===' % (len(lines), secs))
        print('\n'.join(lines[:90])); print('...'); print('\n'.join(lines[-120:]))
        print('=== SMOKE: the first damage and mulligan lines ===')
        print('\n'.join([l for l in lines if l.startswith(('Damage:', 'Mulligan:'))][:12]))
        g = parse(out, pods[0], lib)
        print('=== SMOKE: read from it ===', json.dumps(g)[:3000])
        # Does a seed repeat a game? The same table twice on one seed, once on another: opening hands and outcomes.
        for label, sd in (('seed 4242', 4242), ('seed 4242 again', 4242), ('seed 99', 99)):
            o2, _ = play(work, pods[0], 2, quiet=False, seed=sd)
            for n, x in enumerate(parse(o2, pods[0], lib)):
                print('=== SEED CHECK %s, game %d: open %s | top %s | winner %s | turns %s | lost %s' % (label, n + 1, sorted(x['open']), x['top'], x['winner'], x['turns'], x['lost_order']), flush=True)
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
            mask = mask_of.get(id(ds), 0); mine = swapped(work, mask)
            seats = [mine if d == DECK else d for d in ds]
            seed = seed_of.get(id(ds))
            global LOGMORE
            def sit(n, sd, limit):
                global LOGMORE
                out = play(work, seats, n, quiet=False, slot=k, limit=limit, seed=sd)[0]
                gs = parse(out, seats, lib, mine)
                if LOGMORE and not gs and 'TABLE STOPPED' not in out and re.search(r'Exception|Error', out[-3000:]):
                    print('fuller log: a table failed with it, playing on without:', out[-400:].replace('\n', ' | '), flush=True)
                    LOGMORE = None
                    out = play(work, seats, n, quiet=False, slot=k, limit=limit, seed=sd)[0]
                    gs = parse(out, seats, lib, mine)
                return out, gs
            if PERGAME and seed and MODE == 'test':
                gs = []; out = ''
                for gi in range(GAMES):
                    # No new game after the budget either; the one playing gets the grace, as a table did.
                    if BUDGET and gi and time.time() - t0 > BUDGET * 60: break
                    left = (BUDGET + GRACE) * 60 - (time.time() - t0) if BUDGET else None
                    sd = game_seed(seed, gi)
                    out, g1 = sit(1, sd, left)
                    for g in g1[:1]: g['game'] = gi; g['seed'] = sd; gs.append(g)
                    if 'TABLE STOPPED' in out: break
            else:
                out, gs = sit(GAMES, seed, left)
            with lock:
                if len(gs) < GAMES: bad.append({'decks': ds, 'games_read': len(gs), 'tail': out[-800:]})
                for g in gs:
                    # Whatever combination it played, the record names our deck the same way.
                    if g['winner'] == mine: g['winner'] = DECK
                    g['decks'] = ds; g['mask'] = mask; g['table'] = index_of.get(id(ds)); recs.append(g)
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

def draw_results(mine, deck_cards):
    """Per card, over the games whose log had the draws: [name, games it was in the opening hand we kept, of which won,
    games it was among the top cards of the shuffled deck (that hand and the next TOP draws), of which won]. Which
    cards those are is the shuffle's doing, not the game's, so the difference is the card's own effect."""
    gs = [g for g in mine if g.get('dl')]
    rows = []
    for c in deck_cards:
        o = [g for g in gs if c in g['open']]; t = [g for g in gs if c in g['open'] or c in g['top']]
        rows.append([c, len(o), sum(g['me_won'] for g in o), len(t), sum(g['me_won'] for g in t)])
    return gs, rows

def why(gs):
    """The games we won beside the ones we did not, on what happened early: for reading what the deck's wins hang on."""
    def side(xs):
        if not xs: return None
        n = len(xs); avg = lambda f: round(sum(f(g) for g in xs) / n, 2)
        cmd = [g['cmd_turn'] for g in xs if g['cmd_turn'] is not None]
        return {'games': n, 'mulligans': avg(lambda g: g['mull']), 'lands_by_turn_6': avg(lambda g: g['lands_by_t6']),
                'commander_cast_pct': round(100 * len(cmd) / n, 1), 'commander_turn': round(sum(cmd) / len(cmd), 2) if cmd else None,
                'damage_taken_by_turn_6': avg(lambda g: g['hit6']), 'damage_taken': avg(lambda g: g['hit']), 'our_turns': avg(lambda g: g['my_turns'])}
    return {'won': side([g for g in gs if g['me_won']]), 'not_won': side([g for g in gs if not g['me_won']])}

def place_of(g):
    """0 won, 1 second, 2 third, 3 first out; None for a game that was not played out."""
    if g['me_won']: return 0
    if g['me'] in g['lost_order'] and g['lost_order'].index(g['me']) < 3: return 3 - g['lost_order'].index(g['me'])
    return None

def levers(mine):
    """What the deck's results hang on: the games split by what happened early, each group as
    [games, won, second, third, first out]. Fixed cut points, so the groups of several runs add up."""
    def split(gs, f, n):
        out = [[0, 0, 0, 0, 0] for _ in range(n)]
        for g in gs:
            k = f(g); p = place_of(g)
            if k is None or p is None: continue
            out[k][0] += 1; out[k][1 + p] += 1
        return out
    dl = [g for g in mine if g.get('dl')]
    # Only games in which the deck had the turns the count is over. A game it was knocked out of on its turn 4 has
    # few lands, no commander and little damage by its turn 6 because it ended, not the other way round: counted in,
    # such games make "too few lands" look like the reason for a loss that came first.
    six = lambda gs: [g for g in gs if g['my_turns'] >= 6]
    cmd = lambda g: 2 if g['cmd_turn'] is None or g['cmd_turn'] >= 7 else 0 if g['cmd_turn'] <= 4 else 1
    out = {
        'lands': {'groups': ['4 or fewer lands by its turn 6', '5 lands', '6 or more'], 'rows': split(six(mine), lambda g: 0 if g['lands_by_t6'] <= 4 else 1 if g['lands_by_t6'] == 5 else 2, 3)},
        'commander': {'groups': ['commander by its turn 4', 'on turn 5 or 6', 'later or never'], 'rows': split(six(mine), cmd, 3)},
        'early': {'groups': ['no or one spell by its turn 4', '2 or 3 spells', '4 or more'], 'rows': split([g for g in mine if g['my_turns'] >= 4], lambda g: 0 if g['early'] <= 1 else 1 if g['early'] <= 3 else 2, 3)},
    }
    if len(dl) >= 20:
        out['mulligan'] = {'groups': ['kept its first hand', 'took a mulligan'], 'rows': split(dl, lambda g: 0 if not g['mull'] else 1, 2)}
        out['pressure'] = {'groups': ['under 5 damage taken by its turn 6', '5 to 14', '15 or more'], 'rows': split(six(dl), lambda g: 0 if g['hit6'] < 5 else 1 if g['hit6'] < 15 else 2, 3)}
    return out

def first_cards(mine):
    """Game by game, where the deck finished and which cards were the top of its shuffled deck (the opening hand it
    kept and the next TOP draws): {'cards': [names], 'rows': [[place, card, card, ...]]} with cards as places in the
    list. The shuffle decides these, so they say what kind of card the deck does better for having early."""
    names = {}; rows = []
    for g in mine:
        p = place_of(g)
        if not g.get('dl') or p is None: continue
        rows.append([p] + [names.setdefault(c, len(names)) for c in g['open'] + g['top']])
    return {'cards': list(names), 'rows': rows}

def game_rows(mine):
    """Game by game: [table, game of the table, swaps mask, place]. With every game on its own seed, the same game of
    another run of this deck was dealt the same way, and replays nearly move for move when the lists are the same
    (checked on two runs of one list: 23 of 24 games had the same winner, all 24 the same place for the deck)."""
    rows = []
    for g in mine:
        p = place_of(g)
        if g.get('table') is None or g.get('game') is None: continue
        rows.append([g['table'], g['game'], g.get('mask', 0), -1 if p is None else p])   # -1: not played out (a draw, the clock)
    return sorted(rows)

def held_results(mine, deck_cards):
    """Per card: [name, games it was among the top cards of the shuffled deck and the deck had HELD_TURNS turns or more,
    of which it was played]. A card the deck held all game and never played is one the computer cannot use, which is
    not the same as a card it plays and loses with."""
    gs = [g for g in mine if g.get('dl') and g['my_turns'] >= HELD_TURNS]
    rows = []
    for c in deck_cards:
        h = [g for g in gs if c in g['open'] or c in g['top']]
        rows.append([c, len(h), sum(1 for g in h if c in g['cast'])])
    return rows
HELD_TURNS = 8

def field_rows(mine, lib):
    """The tables our deck sat at, by the three other decks: [deck, deck, deck, games with a winner, of which our deck
    won, won by the first, the second, the third]. What a strength per official deck is fitted on, over every run."""
    by = {}
    for g in mine:
        if not g.get('winner'): continue
        opp = sorted(lib.get(d, d) for d in g['decks'] if d != DECK)
        if len(opp) != 3: continue
        r = by.setdefault(tuple(opp), [*opp, 0, 0, 0, 0, 0]); r[3] += 1
        if g['me_won']: r[4] += 1
        else:
            w = lib.get(g['winner'], g['winner'])
            if w in opp: r[5 + opp.index(w)] += 1
    return sorted(by.values())

def table_rows(mine):
    by = {}
    for g in mine:
        p = place_of(g)
        if g.get('table') is None or p is None: continue
        r = by.setdefault(g['table'], [g['table'], g.get('mask', 0), 0, 0, 0, 0, 0]); r[2] += 1; r[3 + p] += 1
    return sorted(by.values())

def places(mine):
    out = [0, 0, 0, 0]
    for g in mine:
        if g['me_won']: out[0] += 1
        elif g['me'] in g['lost_order']:
            i = g['lost_order'].index(g['me'])
            if i < 3: out[3 - i] += 1
    return out

def versus(mine, lib):
    rows = {}
    for g in mine:
        order = g['lost_order']; mine_at = order.index(g['me']) if g['me'] in order else None
        for seat, d in enumerate(g['decks'], 1):
            if d == DECK: continue
            r = rows.setdefault(d, [lib.get(d, d), 0, 0, 0, 0]); r[1] += 1
            if g['me_won']: r[2] += 1
            if g['winner'] == d: r[3] += 1
            # Out before it: our deck lost and that deck was still in the game then.
            if mine_at is not None and (seat not in order or order.index(seat) > mine_at): r[4] += 1
    return sorted(rows.values(), key=lambda r: -r[1])

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
    summary = {'mode': MODE, 'pool': POOL, 'deck': DECK, 'pods': PODS, 'games_per_pod': GAMES, 'clock_s': CLOCK, 'profile': PROFILE or 'default', 'mc': MC or 'off',
               'machines': len(parts), 'machines_expected': SHARDS, 'workers': WORKERS, 'games': total, 'games_expected': PODS * GAMES, 'draws_or_timeouts': draws,
               'machine_minutes': [p['minutes'] for p in parts], 'budget_min': BUDGET, 'skip': SKIP,
               'tables_not_started': sum(p.get('skipped', 0) for p in parts),
               'game_seconds_median': round(ms[len(ms) // 2] / 1000, 1) if ms else None,
               'game_seconds_p90': round(ms[int(len(ms) * .9)] / 1000, 1) if ms else None, 'decks': rows, 'groups': groups if MODE == 'ladder' else None, 'average': AVERAGE or None, 'incomplete_pods': bad[:5]}
    # The cards Forge's computer has no play for: the whole list for the starter, and the tested deck's own in the summary.
    try: blank = ai_cannot()
    except Exception as e: blank = []; print('cards without computer play not read: %s' % e)
    summary['forge'] = forge_version()
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
            # Where our deck finished: [won, second, third, first out]; games that ended in a draw are in none.
            'places': places(mine),
            'win_turn_median': med([g['turns'] for g in mine if g['me_won']]),
            # Per opponent: [name, games at one table with it, of which our deck won, of which it won, games our deck was out before it].
            'vs': versus(mine, lib),
            'levers': levers(mine),
            'field': field_rows(mine, lib),
            'ai_cannot': sorted(set(blank) & set(deck_names(work))),
            # Per table of the deal (its place in it, the swaps it played): [table, mask, games, won, second, third, first out].
            # The same table of another run of this deck started from the same shuffles (seeded).
            'tables': table_rows(mine),
            # Game by game, when each had its own seed: [table, game, mask, place].
            'per_game': game_rows(mine) if PERGAME else None,
        }
        if SWAPS and any(g.get('mask') for g in mine):
            # Per combination of swaps played: [mask, games, of which won, second, third, first out].
            by = {}
            for g in mine:
                r = by.setdefault(g.get('mask', 0), [g.get('mask', 0), 0, 0, 0, 0, 0]); r[1] += 1
                if g['me_won']: r[2] += 1
                elif g['me'] in g['lost_order'] and g['lost_order'].index(g['me']) < 3: r[5 - g['lost_order'].index(g['me'])] += 1
            summary['our_deck']['trial'] = {'swaps': SWAPS, 'rows': sorted(by.values())}
        gs, rows = draw_results(mine, deck_cards)
        if len(gs) >= 20:
            dealt = {}
            for g in gs:
                for c, d in g['dealt'].items(): x = dealt.setdefault(c, [c, 0, 0]); x[1] += 1; x[2] += d
            summary['our_deck'].update({
                # Games whose log had the draws, and how many of them we won: what `draws` is counted over.
                'draw_games': len(gs), 'draw_wins': sum(g['me_won'] for g in gs), 'top_cards': 7 + TOP, 'draws': rows,
                'mulligans_avg': round(sum(g['mull'] for g in gs) / len(gs), 2),
                # Damage our cards dealt to the other players: [card, games it dealt any, damage in all].
                'dealt': sorted(dealt.values(), key=lambda x: -x[2])[:25],
                'why': why(gs),
                'first': first_cards(mine),
                'held_turns': HELD_TURNS, 'held': held_results(mine, deck_cards),
            })
    print('=== DECK SIM SUMMARY ==='); print(json.dumps(summary, indent=1)); print('=== END SUMMARY ===')
    try: print_ai_cannot(blank)
    except Exception as e: print('list of cards without computer play not printed: %s' % e)
    try: print_games(parts, lib, summary)
    except Exception as e: print('games not printed: %s' % e)   # never at the summary's cost

def deck_names(work):
    """Every card name in the tested deck's file, the commander included."""
    out = []
    for l in open(os.path.join(work, DECK), encoding='utf-8').read().splitlines():
        m = re.match(r'^\d+ (.+?)(\|.*)?$', l.strip())
        if m: out.append(m.group(1).strip())
    return out

def forge_version():
    m = re.search(r'forge-gui-desktop-(.+?)-jar-with-dependencies', os.path.basename(jar()))
    return m.group(1) if m else ''

def ai_cannot():
    """Every card Forge's own script marks as one its computer player has no play for (the line AI:RemoveDeck:All),
    by the name a deck file gives it: a split card by both halves, any other by its first name. Counterspells are
    left out, as the computer does play those (measured: Swan Song in 11 of 24 games). Such a card is a
    blank in a test, whoever holds it, so whoever starts the runs wants the list before building a deck."""
    import zipfile
    names = set()
    def scan(text):
        if not re.search(r'^AI:RemoveDeck:All\s*$', text, re.M): return
        # A counterspell is the exception: Forge picks its answers to a spell on the stack before it drops the marked cards.
        if re.search(r'^A:(?:SP|AB)\$ Counter\b', text, re.M): return
        ns = [n.strip() for n in re.findall(r'^Name:(.+)$', text, re.M)]
        if not ns: return
        names.add(' // '.join(ns[:2]) if re.search(r'^AlternateMode:\s*Split\s*$', text, re.M) and len(ns) > 1 else ns[0])
    z = os.path.join(FORGE, 'res', 'cardsfolder', 'cardsfolder.zip')
    if os.path.exists(z):
        with zipfile.ZipFile(z) as f:
            for n in f.namelist():
                if n.endswith('.txt'): scan(f.read(n).decode('utf-8', 'replace'))
    else:
        for q in glob.glob(os.path.join(FORGE, 'res', 'cardsfolder', '**', '*.txt'), recursive=True):
            scan(open(q, encoding='utf-8', errors='replace').read())
    return sorted(names)

def print_ai_cannot(names):
    """The whole list, for the starter to keep: gzipped JSON {forge, names} as base64 between two markers."""
    import base64, gzip
    b64 = base64.b64encode(gzip.compress(json.dumps({'forge': forge_version(), 'names': names}, ensure_ascii=False).encode('utf-8'), 9)).decode('ascii')
    print('=== AI CANNOT gz64 %d ===' % len(names))
    for i in range(0, len(b64), 3000): print(b64[i:i + 3000])
    print('=== END AI CANNOT ===', flush=True)

def print_games(parts, lib, summary):
    """Every game as it was read, for whoever started the run to keep: one line of JSON for the run, then one per
    game (the decks by seat, the winner, who went out in which order, and for the tested deck what it drew, played and
    took), gzipped and printed as base64 between two markers. Nothing is stored here."""
    import base64, gzip
    head = {'v': 1, 'kind': 'run', **{k: summary.get(k) for k in ('mode', 'pool', 'pods', 'games_per_pod', 'clock_s', 'profile', 'workers', 'budget_min', 'skip', 'average', 'games')},
            'seed': SEED, 'seeded_tables': FSEED, 'seeded_games': PERGAME, 'swaps': SWAPS, 'top_draws': TOP, 'commander': COMMANDER}
    lines = [json.dumps(head, separators=(',', ':'))]
    for g in (g for p in parts for g in p['games']):
        r = dict(g); r['decks'] = [lib.get(d, d) for d in g.get('decks') or []]
        r['winner'] = lib.get(g['winner'], g['winner']) if g.get('winner') else None
        if FSEED and g.get('table') is not None and 'seed' not in r: r['seed'] = table_seed(g['table'])
        lines.append(json.dumps(r, separators=(',', ':')))
    blob = base64.b64encode(gzip.compress(('\n'.join(lines) + '\n').encode('utf-8'), 9)).decode('ascii')
    print('=== GAMES gz64 %d ===' % (len(lines) - 1))
    for i in range(0, len(blob), 3000): print(blob[i:i + 3000])
    print('=== END GAMES ===')

def main():
    work, lib = prepare()
    if STAGE in ('play', 'all'): play_stage(work, lib)
    if STAGE in ('merge', 'all'): merge_stage(work, lib)

main()

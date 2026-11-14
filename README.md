# commander-deck-sim

Runs four-player Commander games, computer against computer, to see how a decklist holds up against official
preconstructed decks.

- `run.py` plays the games with the simulation mode of [Forge](https://github.com/Card-Forge/forge), the open-source
  Magic: The Gathering rules engine, and prints a summary: win rates with their 95 % interval, game length, and for the
  tested deck the turn its commander was cast, lands played by turn 6 and how often each card was played.
- After the summary it prints every game as it was read (the decks by seat, the winner, the order the others went out, and for the tested deck what it drew, played and took), gzipped, for whoever started the run to keep. Nothing is stored here.
- In a test every game is played on a seed of its own (one start of Forge per game), so the same game of a later run of the deck is dealt the same way and two lists can be compared game by game. `PERGAME=0` goes back to one start per table.
- Every run also prints the names of the cards Forge's own scripts mark as ones its computer player has no play for (`AI:RemoveDeck:All`), and the summary names the tested deck's own: such a card is a blank in a test.
- The decklist under test is passed in when a run is started, in Forge's `.dck` format; none is stored here. The
  precons are the lists Forge ships, in pools: `standard` (the ordinary precons of late 2023 to 2026), `bracket2` (the same
  without the five that are above Commander Bracket 2 by Wizards' requirements: a Game Changer, mass land denial, a
  turn-chaining card or a two-card game-deciding combo), `strong` (premium lines) and `classic` (the first twelve).
- `.github/workflows/sim.yml` runs it on GitHub Actions, by hand only: up to 20 machines play their share of the tables side by side, then one job merges their games into the summary. Forge's release is kept in the Actions cache.

Forge is downloaded as its own release at run time; none of its code is in this repository. Forge's game log leaves out which cards a player draws, so a test compiles one file of that release's own source (its log writer, fetched at run time) with draws and the player behind each damage added, and runs the release with it in front; if that fails, the release runs as it is and the summary has no draw figures. Results are computer play:
they compare decks under the same AI and say nothing certain about a real table.

This repository exists only to run these simulations. It holds no secrets and takes no contributions.

Magic: The Gathering is a trademark of Wizards of the Coast. This project is not affiliated with or endorsed by Wizards
of the Coast.

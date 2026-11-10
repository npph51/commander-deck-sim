# commander-deck-sim

Runs four-player Commander games, computer against computer, to see how a decklist holds up against official
preconstructed decks.

- `run.py` plays the games with the simulation mode of [Forge](https://github.com/Card-Forge/forge), the open-source
  Magic: The Gathering rules engine, and prints a summary: win rates with their 95 % interval, game length, and for the
  tested deck the turn its commander was cast, lands played by turn 6 and how often each card was played.
- The decklist under test is passed in when a run is started, in Forge's `.dck` format; none is stored here. The
  precons are the lists Forge ships.
- `.github/workflows/sim.yml` runs it on GitHub Actions, by hand only.

Forge is downloaded as its own release at run time; none of its code is in this repository. Results are computer play:
they compare decks under the same AI and say nothing certain about a real table.

This repository exists only to run these simulations. It holds no secrets and takes no contributions.

Magic: The Gathering is a trademark of Wizards of the Coast. This project is not affiliated with or endorsed by Wizards
of the Coast.

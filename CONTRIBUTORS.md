# Contributors

- **[PoriyaVali](https://github.com/PoriyaVali)** — project owner. Found the problem on a
  real UFI Box installation, drove the investigation, tested every build against the
  hardware, and decided what the tool had to become.

- **Claude (Opus 5, Anthropic)** — paired on this from the first crash dump to the
  installer: diagnosed the fault (protection stub decoder vs. the `movups` in the 24H2
  `GetClientRect` prologue), designed the trampoline patch, and wrote the tool, the
  installer and the documentation.

The original version 1 commit retains its historical co-author credit.

Found a bug, or a UFI feature that still crashes? Open an issue with the contents of
`%LOCALAPPDATA%\UFI-Fix\UFI-Fix.log` — it almost certainly means another Windows function
carries the same instruction pattern and needs to be covered.

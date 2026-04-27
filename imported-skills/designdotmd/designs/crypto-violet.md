---
version: alpha
name: Crypto Violet
description: Web3 violet: holo gradients, mono addresses.
colors:
  primary: "#ECE4FF"
  secondary: "#8F85B8"
  tertiary: "#9B5CF6"
  neutral: "#120A24"
  surface: "#1A1138"
  on-primary: "#120A24"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 3.75rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 2rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.04em"
rounded:
  sm: 6px
  md: 12px
  lg: 20px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A web3 wallet palette: deep violet surfaces, gradient primary, monospace addresses.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#ECE4FF`):** Headlines and core text.
- **Secondary (`#8F85B8`):** Borders, captions, and metadata.
- **Tertiary (`#9B5CF6`):** The sole driver for interaction. Reserve it.
- **Neutral (`#120A24`):** The page foundation.

## Typography

- **display:** Space Grotesk 3.75rem
- **h1:** Space Grotesk 2rem
- **body:** Inter 0.95rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

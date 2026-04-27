---
version: alpha
name: Speedrun Clock
description: Split timer aesthetic: green splits, red losses, mono forever.
colors:
  primary: "#E8E8E8"
  secondary: "#888888"
  tertiary: "#22E09A"
  neutral: "#0A0A0A"
  surface: "#121212"
  on-primary: "#0A0A0A"
typography:
  display:
    fontFamily: JetBrains Mono
    fontSize: 3.5rem
    fontWeight: 700
  h1:
    fontFamily: JetBrains Mono
    fontSize: 1.8rem
    fontWeight: 600
  body:
    fontFamily: JetBrains Mono
    fontSize: 0.92rem
    lineHeight: 1.5
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.7rem
    letterSpacing: "0.04em"
rounded:
  sm: 2px
  md: 3px
  lg: 4px
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

A speedrun-timer system. High-contrast mono, green gains, red losses.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E8E8E8`):** Headlines and core text.
- **Secondary (`#888888`):** Borders, captions, and metadata.
- **Tertiary (`#22E09A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0A0A0A`):** The page foundation.

## Typography

- **display:** JetBrains Mono 3.5rem
- **h1:** JetBrains Mono 1.8rem
- **body:** JetBrains Mono 0.92rem
- **label:** JetBrains Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

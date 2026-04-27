---
version: alpha
name: Pixel Quest
description: 8-bit health bars and CRT ink.
colors:
  primary: "#1E1E2E"
  secondary: "#7A7A99"
  tertiary: "#F4C430"
  neutral: "#F6EED6"
  surface: "#FFFDF5"
  on-primary: "#1E1E2E"
typography:
  display:
    fontFamily: Press Start 2P
    fontSize: 2.6rem
    fontWeight: 400
  h1:
    fontFamily: Press Start 2P
    fontSize: 1.3rem
    fontWeight: 400
  body:
    fontFamily: VT323
    fontSize: 1.25rem
    lineHeight: 1.5
  label:
    fontFamily: Press Start 2P
    fontSize: 0.65rem
rounded:
  sm: 0px
  md: 0px
  lg: 0px
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

A pixel-perfect retro system for cozy RPGs — flat fills, chunky frames.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1E1E2E`):** Headlines and core text.
- **Secondary (`#7A7A99`):** Borders, captions, and metadata.
- **Tertiary (`#F4C430`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F6EED6`):** The page foundation.

## Typography

- **display:** Press Start 2P 2.6rem
- **h1:** Press Start 2P 1.3rem
- **body:** VT323 1.25rem
- **label:** Press Start 2P 0.65rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

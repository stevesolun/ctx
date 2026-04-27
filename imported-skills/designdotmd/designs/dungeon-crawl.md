---
version: alpha
name: Dungeon Crawl
description: Torchlight, parchment, dice rolls in the dark.
colors:
  primary: "#E8DCC0"
  secondary: "#8A7A5E"
  tertiary: "#B83A2E"
  neutral: "#1A1612"
  surface: "#241E17"
  on-primary: "#E8DCC0"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 4.5rem
    fontWeight: 500
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: EB Garamond
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: IM Fell English
    fontSize: 0.82rem
    letterSpacing: "0.08em"
rounded:
  sm: 2px
  md: 4px
  lg: 6px
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

A tabletop-inspired system for dark-fantasy games. Charcoal backgrounds, parchment cards.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E8DCC0`):** Headlines and core text.
- **Secondary (`#8A7A5E`):** Borders, captions, and metadata.
- **Tertiary (`#B83A2E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#1A1612`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 4.5rem
- **h1:** Cormorant Garamond 2.5rem
- **body:** EB Garamond 1.05rem
- **label:** IM Fell English 0.82rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

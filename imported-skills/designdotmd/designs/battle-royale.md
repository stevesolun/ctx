---
version: alpha
name: Battle Royale
description: Drop-in HUD: blood-orange alerts, squad chevrons.
colors:
  primary: "#F2F2F0"
  secondary: "#7A7E85"
  tertiary: "#FF5A1F"
  neutral: "#0B0C0F"
  surface: "#141619"
  on-primary: "#0B0C0F"
typography:
  display:
    fontFamily: Teko
    fontSize: 5rem
    fontWeight: 700
    letterSpacing: "0.02em"
  h1:
    fontFamily: Teko
    fontSize: 2.6rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.5
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
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

A competitive-shooter HUD palette: near-black surface, orange alerts, squad-color chevrons.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F2F2F0`):** Headlines and core text.
- **Secondary (`#7A7E85`):** Borders, captions, and metadata.
- **Tertiary (`#FF5A1F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0B0C0F`):** The page foundation.

## Typography

- **display:** Teko 5rem
- **h1:** Teko 2.6rem
- **body:** Inter 0.92rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

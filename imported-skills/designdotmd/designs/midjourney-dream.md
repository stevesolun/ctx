---
version: alpha
name: Dream Engine
description: Generative AI: obsidian surface, dream violet, seed amber.
colors:
  primary: "#F1EDF9"
  secondary: "#8C87A2"
  tertiary: "#C18CFF"
  neutral: "#0B0A13"
  surface: "#15131F"
  on-primary: "#0B0A13"
typography:
  display:
    fontFamily: Instrument Serif
    fontSize: 5rem
    fontWeight: 400
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Instrument Serif
    fontSize: 2.6rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.6
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.06em"
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

A generative-AI palette: obsidian dark surface, dream-violet accent, seed-amber parameter chips.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F1EDF9`):** Headlines and core text.
- **Secondary (`#8C87A2`):** Borders, captions, and metadata.
- **Tertiary (`#C18CFF`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0B0A13`):** The page foundation.

## Typography

- **display:** Instrument Serif 5rem
- **h1:** Instrument Serif 2.6rem
- **body:** Inter 0.95rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

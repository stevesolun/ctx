---
version: alpha
name: Tavern Tabletop
description: Tabletop tavern: parchment map, wax-seal red, inked routes.
colors:
  primary: "#22160E"
  secondary: "#8A765C"
  tertiary: "#B32A2A"
  neutral: "#F0E1C6"
  surface: "#F8EACC"
  on-primary: "#F8EACC"
typography:
  display:
    fontFamily: IM Fell English
    fontSize: 4.5rem
    fontWeight: 400
    letterSpacing: "-0.01em"
  h1:
    fontFamily: IM Fell English
    fontSize: 2.4rem
    fontWeight: 400
  body:
    fontFamily: Lora
    fontSize: 1.02rem
    lineHeight: 1.7
  label:
    fontFamily: IM Fell English
    fontSize: 0.82rem
    letterSpacing: "0.1em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

A board-game palette: parchment-map surface, wax-seal red, inked-route secondary.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#22160E`):** Headlines and core text.
- **Secondary (`#8A765C`):** Borders, captions, and metadata.
- **Tertiary (`#B32A2A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0E1C6`):** The page foundation.

## Typography

- **display:** IM Fell English 4.5rem
- **h1:** IM Fell English 2.4rem
- **body:** Lora 1.02rem
- **label:** IM Fell English 0.82rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

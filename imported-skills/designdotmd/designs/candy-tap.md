---
version: alpha
name: Candy Tap
description: Soft gradients, chewy taps, confetti everywhere.
colors:
  primary: "#3A1F5C"
  secondary: "#B794D6"
  tertiary: "#FF5AAD"
  neutral: "#FFF5FB"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Nunito
    fontSize: 4rem
    fontWeight: 900
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Nunito
    fontSize: 2rem
    fontWeight: 800
  body:
    fontFamily: Nunito
    fontSize: 1rem
    lineHeight: 1.55
  label:
    fontFamily: Nunito
    fontSize: 0.78rem
    fontWeight: 700
    letterSpacing: "0.04em"
rounded:
  sm: 12px
  md: 20px
  lg: 32px
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

A saccharine puzzle-game palette. Milky pastels with one juicy magenta for combos.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#3A1F5C`):** Headlines and core text.
- **Secondary (`#B794D6`):** Borders, captions, and metadata.
- **Tertiary (`#FF5AAD`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FFF5FB`):** The page foundation.

## Typography

- **display:** Nunito 4rem
- **h1:** Nunito 2rem
- **body:** Nunito 1rem
- **label:** Nunito 0.78rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

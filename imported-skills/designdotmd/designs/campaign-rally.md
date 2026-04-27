---
version: alpha
name: Campaign Rally
description: Campaign signage: stars blue, bunting red, placard white.
colors:
  primary: "#0B1F44"
  secondary: "#5B6C8A"
  tertiary: "#C22434"
  neutral: "#F4F5F8"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Oswald
    fontSize: 5.5rem
    fontWeight: 700
    letterSpacing: "0.02em"
  h1:
    fontFamily: Oswald
    fontSize: 2.6rem
    fontWeight: 700
  body:
    fontFamily: Source Serif 4
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Oswald
    fontSize: 0.82rem
    letterSpacing: "0.18em"
rounded:
  sm: 0px
  md: 2px
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

A political-campaign palette: stars-blue primary, bunting-red accent, placard-white surfaces.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0B1F44`):** Headlines and core text.
- **Secondary (`#5B6C8A`):** Borders, captions, and metadata.
- **Tertiary (`#C22434`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4F5F8`):** The page foundation.

## Typography

- **display:** Oswald 5.5rem
- **h1:** Oswald 2.6rem
- **body:** Source Serif 4 1rem
- **label:** Oswald 0.82rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

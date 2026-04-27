---
version: alpha
name: Running Kilometer
description: Runner app: track orange, split white, PR green.
colors:
  primary: "#151818"
  secondary: "#6A7272"
  tertiary: "#FF5E1A"
  neutral: "#F4F5F2"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Outfit
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Outfit
    fontSize: 2.2rem
    fontWeight: 700
  body:
    fontFamily: Outfit
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Outfit
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.12em"
rounded:
  sm: 8px
  md: 14px
  lg: 24px
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

A running-app palette: track-orange primary, clean white surfaces, PR-green accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#151818`):** Headlines and core text.
- **Secondary (`#6A7272`):** Borders, captions, and metadata.
- **Tertiary (`#FF5E1A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4F5F2`):** The page foundation.

## Typography

- **display:** Outfit 4rem
- **h1:** Outfit 2.2rem
- **body:** Outfit 0.95rem
- **label:** Outfit 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

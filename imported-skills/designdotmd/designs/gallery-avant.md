---
version: alpha
name: Gallery Avant
description: Contemporary-art gallery: white cube, tight caps.
colors:
  primary: "#0A0A0A"
  secondary: "#7A7A7A"
  tertiary: "#3A3A3A"
  neutral: "#F8F8F7"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Italiana
    fontSize: 6rem
    fontWeight: 400
    letterSpacing: "0.02em"
  h1:
    fontFamily: Italiana
    fontSize: 2.8rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.68rem
    fontWeight: 500
    letterSpacing: "0.28em"
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

A contemporary-art gallery system: white-cube surface, all-caps sans micro labels.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0A0A0A`):** Headlines and core text.
- **Secondary (`#7A7A7A`):** Borders, captions, and metadata.
- **Tertiary (`#3A3A3A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F8F8F7`):** The page foundation.

## Typography

- **display:** Italiana 6rem
- **h1:** Italiana 2.8rem
- **body:** Inter 0.92rem
- **label:** Inter 0.68rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

---
version: alpha
name: Coffee Roast
description: Third-wave roaster: espresso brown, kraft paper, latte.
colors:
  primary: "#2B1810"
  secondary: "#8A7260"
  tertiary: "#D97742"
  neutral: "#EBE0CE"
  surface: "#F7EEDC"
  on-primary: "#F7EEDC"
typography:
  display:
    fontFamily: Fraunces
    fontSize: 4.5rem
    fontWeight: 600
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.98rem
    lineHeight: 1.6
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.14em"
rounded:
  sm: 4px
  md: 8px
  lg: 14px
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

A speciality-coffee system.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2B1810`):** Headlines and core text.
- **Secondary (`#8A7260`):** Borders, captions, and metadata.
- **Tertiary (`#D97742`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EBE0CE`):** The page foundation.

## Typography

- **display:** Fraunces 4.5rem
- **h1:** Fraunces 2.4rem
- **body:** Inter 0.98rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

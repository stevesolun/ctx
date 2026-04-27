---
version: alpha
name: Pet Pawprint
description: Pet care: biscuit tan, wag blue, squeak yellow.
colors:
  primary: "#2A1E14"
  secondary: "#8D7A63"
  tertiary: "#FFB72B"
  neutral: "#F5EBD8"
  surface: "#FDF4E1"
  on-primary: "#FDF4E1"
typography:
  display:
    fontFamily: Fraunces
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.025em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.2rem
    fontWeight: 600
  body:
    fontFamily: DM Sans
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: DM Sans
    fontSize: 0.74rem
    fontWeight: 600
    letterSpacing: "0.08em"
rounded:
  sm: 10px
  md: 18px
  lg: 28px
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

A pet-product palette: biscuit tan surface, wag-blue primary, squeaky-yellow accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2A1E14`):** Headlines and core text.
- **Secondary (`#8D7A63`):** Borders, captions, and metadata.
- **Tertiary (`#FFB72B`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F5EBD8`):** The page foundation.

## Typography

- **display:** Fraunces 4rem
- **h1:** Fraunces 2.2rem
- **body:** DM Sans 1rem
- **label:** DM Sans 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

---
version: alpha
name: Florist Bouquet
description: Boutique florist: petal blush, stem green, kraft paper.
colors:
  primary: "#221D18"
  secondary: "#8B8070"
  tertiary: "#E58BA3"
  neutral: "#F0E7D6"
  surface: "#F8EEDC"
  on-primary: "#F8EEDC"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 5rem
    fontWeight: 400
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.5rem
    fontWeight: 400
  body:
    fontFamily: Lora
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Lora
    fontSize: 0.75rem
    fontWeight: 600
    letterSpacing: "0.2em"
rounded:
  sm: 4px
  md: 10px
  lg: 18px
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

A florist-brand palette: kraft paper surface, petal-blush accent, stem-green secondary.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#221D18`):** Headlines and core text.
- **Secondary (`#8B8070`):** Borders, captions, and metadata.
- **Tertiary (`#E58BA3`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0E7D6`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 5rem
- **h1:** Cormorant Garamond 2.5rem
- **body:** Lora 1rem
- **label:** Lora 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

---
version: alpha
name: Whisky Peat
description: Peated scotch: smoke black, barrel oak, burnt orange.
colors:
  primary: "#F0E6D1"
  secondary: "#A89574"
  tertiary: "#D17A2C"
  neutral: "#0F0B08"
  surface: "#1A130E"
  on-primary: "#F0E6D1"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 5rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Playfair Display
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: Lora
    fontSize: 1.02rem
    lineHeight: 1.7
  label:
    fontFamily: Lora
    fontSize: 0.74rem
    fontWeight: 600
    letterSpacing: "0.2em"
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

A whisky-brand palette: peat-smoke black, oak barrel browns, one burnt-orange label accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F0E6D1`):** Headlines and core text.
- **Secondary (`#A89574`):** Borders, captions, and metadata.
- **Tertiary (`#D17A2C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0F0B08`):** The page foundation.

## Typography

- **display:** Playfair Display 5rem
- **h1:** Playfair Display 2.4rem
- **body:** Lora 1.02rem
- **label:** Lora 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

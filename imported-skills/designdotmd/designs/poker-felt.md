---
version: alpha
name: Poker Felt
description: Poker table: felt green, card ivory, chip red.
colors:
  primary: "#EFE6D2"
  secondary: "#A3997F"
  tertiary: "#C22C2C"
  neutral: "#0C2418"
  surface: "#143226"
  on-primary: "#EFE6D2"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 4.5rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Playfair Display
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: Lora
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Lora
    fontSize: 0.75rem
    fontWeight: 600
    letterSpacing: "0.18em"
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

A casino/poker palette: felt-green table, card-ivory surface, chip-red primary accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#EFE6D2`):** Headlines and core text.
- **Secondary (`#A3997F`):** Borders, captions, and metadata.
- **Tertiary (`#C22C2C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0C2418`):** The page foundation.

## Typography

- **display:** Playfair Display 4.5rem
- **h1:** Playfair Display 2.4rem
- **body:** Lora 1rem
- **label:** Lora 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

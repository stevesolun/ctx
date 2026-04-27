---
version: alpha
name: Farmers Market
description: Chalkboard signs, crate wood, tomato red.
colors:
  primary: "#F5F1DE"
  secondary: "#A89C7C"
  tertiary: "#DB3F3A"
  neutral: "#1F2A20"
  surface: "#2A382B"
  on-primary: "#F5F1DE"
typography:
  display:
    fontFamily: Caveat
    fontSize: 5rem
    fontWeight: 700
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: Lora
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Caveat
    fontSize: 1.05rem
    letterSpacing: "0.02em"
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

A farmers-market palette: chalkboard dark, crate-wood warmth, tomato-red accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F5F1DE`):** Headlines and core text.
- **Secondary (`#A89C7C`):** Borders, captions, and metadata.
- **Tertiary (`#DB3F3A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#1F2A20`):** The page foundation.

## Typography

- **display:** Caveat 5rem
- **h1:** Fraunces 2.4rem
- **body:** Lora 1rem
- **label:** Caveat 1.05rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

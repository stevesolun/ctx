---
version: alpha
name: Neobank Mint
description: Fresh mint, generous gutters, calm money.
colors:
  primary: "#0D3B2E"
  secondary: "#6B8679"
  tertiary: "#28C76F"
  neutral: "#F3FAF5"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Manrope
    fontSize: 3.75rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Manrope
    fontSize: 2rem
    fontWeight: 700
  body:
    fontFamily: Manrope
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Manrope
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.04em"
rounded:
  sm: 6px
  md: 12px
  lg: 20px
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

A challenger-bank system that feels like a Sunday morning.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0D3B2E`):** Headlines and core text.
- **Secondary (`#6B8679`):** Borders, captions, and metadata.
- **Tertiary (`#28C76F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F3FAF5`):** The page foundation.

## Typography

- **display:** Manrope 3.75rem
- **h1:** Manrope 2rem
- **body:** Manrope 0.95rem
- **label:** Manrope 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

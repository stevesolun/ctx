---
version: alpha
name: Vercel Ink
description: Surgical black, surgical white, sharp geometry.
colors:
  primary: "#EDEDED"
  secondary: "#8F8F8F"
  tertiary: "#0070F3"
  neutral: "#000000"
  surface: "#0A0A0A"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Geist
    fontSize: 4rem
    fontWeight: 600
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Geist
    fontSize: 2.25rem
    fontWeight: 600
    letterSpacing: "-0.025em"
  body:
    fontFamily: Geist
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Geist Mono
    fontSize: 0.75rem
    letterSpacing: "0"
rounded:
  sm: 6px
  md: 8px
  lg: 12px
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

Product-grade minimalism. Edge-to-edge black surfaces, thin hairlines, a single electric gradient accent reserved for primary action.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#EDEDED`):** Headlines and core text.
- **Secondary (`#8F8F8F`):** Borders, captions, and metadata.
- **Tertiary (`#0070F3`):** The sole driver for interaction. Reserve it.
- **Neutral (`#000000`):** The page foundation.

## Typography

- **display:** Geist 4rem
- **h1:** Geist 2.25rem
- **body:** Geist 0.95rem
- **label:** Geist Mono 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

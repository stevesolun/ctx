---
version: alpha
name: Aqua Mint
description: Seaglass, mint, chilled glass surfaces.
colors:
  primary: "#0F2E2C"
  secondary: "#5A8280"
  tertiary: "#2DD4BF"
  neutral: "#E8F7F3"
  surface: "#FFFFFF"
  on-primary: "#0F2E2C"
typography:
  display:
    fontFamily: DM Sans
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: DM Sans
    fontSize: 2.25rem
    fontWeight: 700
  body:
    fontFamily: DM Sans
    fontSize: 0.95rem
    lineHeight: 1.6
  label:
    fontFamily: DM Mono
    fontSize: 0.72rem
    letterSpacing: "0.06em"
rounded:
  sm: 8px
  md: 14px
  lg: 22px
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

A refreshing product palette. Pale aqua surfaces, deep teal primary, mint accent. Calm and confident.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F2E2C`):** Headlines and core text.
- **Secondary (`#5A8280`):** Borders, captions, and metadata.
- **Tertiary (`#2DD4BF`):** The sole driver for interaction. Reserve it.
- **Neutral (`#E8F7F3`):** The page foundation.

## Typography

- **display:** DM Sans 4rem
- **h1:** DM Sans 2.25rem
- **body:** DM Sans 0.95rem
- **label:** DM Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

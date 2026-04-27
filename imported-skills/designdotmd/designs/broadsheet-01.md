---
version: alpha
name: Broadsheet 01
description: Newsprint ink, column rules, masthead gravitas.
colors:
  primary: "#0F0F0E"
  secondary: "#5E5A54"
  tertiary: "#B21F1F"
  neutral: "#FBF9F2"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Old Standard TT
    fontSize: 4.5rem
    fontWeight: 700
  h1:
    fontFamily: Old Standard TT
    fontSize: 2.5rem
    fontWeight: 700
  body:
    fontFamily: Source Serif 4
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 700
    letterSpacing: "0.14em"
rounded:
  sm: 0px
  md: 0px
  lg: 2px
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

A classical news-site system built on column grids.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F0F0E`):** Headlines and core text.
- **Secondary (`#5E5A54`):** Borders, captions, and metadata.
- **Tertiary (`#B21F1F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FBF9F2`):** The page foundation.

## Typography

- **display:** Old Standard TT 4.5rem
- **h1:** Old Standard TT 2.5rem
- **body:** Source Serif 4 1.05rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

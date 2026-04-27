---
version: alpha
name: Exchange Tick
description: Order book green/red. Zero ornament. Every tick earns.
colors:
  primary: "#E6E9EF"
  secondary: "#6E7786"
  tertiary: "#26A17B"
  neutral: "#0A0D14"
  surface: "#11141D"
  on-primary: "#0A0D14"
typography:
  display:
    fontFamily: IBM Plex Mono
    fontSize: 3rem
    fontWeight: 600
  h1:
    fontFamily: IBM Plex Sans
    fontSize: 1.75rem
    fontWeight: 600
  body:
    fontFamily: IBM Plex Mono
    fontSize: 0.88rem
    lineHeight: 1.5
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.7rem
    letterSpacing: "0.04em"
rounded:
  sm: 2px
  md: 3px
  lg: 5px
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

An exchange order-book aesthetic: ultra-dense tables, mono numerics, bid-ask duotone.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E6E9EF`):** Headlines and core text.
- **Secondary (`#6E7786`):** Borders, captions, and metadata.
- **Tertiary (`#26A17B`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0A0D14`):** The page foundation.

## Typography

- **display:** IBM Plex Mono 3rem
- **h1:** IBM Plex Sans 1.75rem
- **body:** IBM Plex Mono 0.88rem
- **label:** IBM Plex Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

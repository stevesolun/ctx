---
version: alpha
name: Terminal
description: Phosphor green on deep black. Fast, loud, honest.
colors:
  primary: "#E6EDF3"
  secondary: "#8B949E"
  tertiary: "#3FB950"
  neutral: "#0D1117"
  surface: "#161B22"
  on-primary: "#0D1117"
typography:
  display:
    fontFamily: IBM Plex Mono
    fontSize: 3.5rem
    fontWeight: 600
    letterSpacing: "-0.02em"
  h1:
    fontFamily: IBM Plex Mono
    fontSize: 2rem
    fontWeight: 600
  body:
    fontFamily: IBM Plex Mono
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.75rem
    letterSpacing: "0.02em"
rounded:
  sm: 4px
  md: 6px
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

A dev-native palette. Mono everywhere, green on black, single red for destructive. No decoration, only signal.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E6EDF3`):** Headlines and core text.
- **Secondary (`#8B949E`):** Borders, captions, and metadata.
- **Tertiary (`#3FB950`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0D1117`):** The page foundation.

## Typography

- **display:** IBM Plex Mono 3.5rem
- **h1:** IBM Plex Mono 2rem
- **body:** IBM Plex Mono 0.95rem
- **label:** IBM Plex Mono 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

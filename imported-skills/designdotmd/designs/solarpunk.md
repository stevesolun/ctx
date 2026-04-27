---
version: alpha
name: Solarpunk
description: Optimistic eco: plant green, sun yellow, soft terracotta.
colors:
  primary: "#14301E"
  secondary: "#5F7A68"
  tertiary: "#F4C430"
  neutral: "#F0EAD2"
  surface: "#F7F0D8"
  on-primary: "#14301E"
typography:
  display:
    fontFamily: Fraunces
    fontSize: 4.5rem
    fontWeight: 700
    letterSpacing: "-0.025em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: DM Sans
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: DM Sans
    fontSize: 0.74rem
    fontWeight: 600
    letterSpacing: "0.1em"
rounded:
  sm: 10px
  md: 18px
  lg: 28px
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

A solarpunk aesthetic: verdant greens, sunny yellows, organic radii, hand-drawn warmth.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#14301E`):** Headlines and core text.
- **Secondary (`#5F7A68`):** Borders, captions, and metadata.
- **Tertiary (`#F4C430`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0EAD2`):** The page foundation.

## Typography

- **display:** Fraunces 4.5rem
- **h1:** Fraunces 2.4rem
- **body:** DM Sans 1rem
- **label:** DM Sans 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.

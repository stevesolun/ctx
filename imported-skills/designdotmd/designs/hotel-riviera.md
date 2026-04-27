---
version: alpha
name: Hotel Riviera
description: Côte d'Azur: sea-wash blue, sun-bleached sand, soft gold.
colors:
  primary: "#162C3A"
  secondary: "#7B8B97"
  tertiary: "#C9A16A"
  neutral: "#F2ECE0"
  surface: "#FBF7EC"
  on-primary: "#FBF7EC"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 5rem
    fontWeight: 500
  h1:
    fontFamily: Playfair Display
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: Jost
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Jost
    fontSize: 0.72rem
    fontWeight: 500
    letterSpacing: "0.18em"
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

A boutique-hotel system inspired by the Riviera.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#162C3A`):** Headlines and core text.
- **Secondary (`#7B8B97`):** Borders, captions, and metadata.
- **Tertiary (`#C9A16A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F2ECE0`):** The page foundation.

## Typography

- **display:** Playfair Display 5rem
- **h1:** Playfair Display 2.5rem
- **body:** Jost 1rem
- **label:** Jost 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
